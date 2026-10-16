#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Global Quantitative Football Betting Strategy Engine
Kullanılan Matematiksel & Global Modeller:
1. Multiplicative / Shin Vig-Removal (Bahis şirketi kar marjını arındırıp gerçek olasılığı bulma)
2. Inverse Multi-Market Poisson Solver (Alt/Üst 1.5, 2.5, 3.5, Ev/Dep 1.5 ve MS oranlarından xG / Lambda çıkarma)
3. Dixon-Coles (1997) Bivariate Poisson 9x9 Skor Matrisi (Düşük skor korelasyon düzeltmeli kesin skor dağılımı)
4. Half-Split Tempo Modeli (1. Yarı %44 / 2. Yarı %56 gol dağılımı + İY market kalibrasyonu -> İki Yarı KG & İY 1.5 Üst)
5. Fat-Tail Volatility / Chaos Index (+6 Gol ve 3.5+ Üst için yüksek varyans ve kontra-atak endeksi)
6. Positive Expected Value (+EV) & Fractional Kelly Criterion (Oran sapması / Değerli bahis tespiti)
"""

import math
from typing import Any, Dict, List, Optional, Tuple


def poisson_pmf(k: int, lam: float) -> float:
    """P(X = k) Poisson olasılık kütle fonksiyonu."""
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def poisson_over_prob(threshold: float, lam: float) -> float:
    """P(X > threshold) örn. threshold=2.5 için P(X >= 3)."""
    max_k = int(math.floor(threshold))
    under_prob = sum(poisson_pmf(k, lam) for k in range(max_k + 1))
    return max(0.0, min(1.0, 1.0 - under_prob))


def solve_lambda_from_over_prob(target_prob: float, threshold: float = 2.5) -> float:
    """
    Verilen Üst olasılığından (örn. P(Gol >= 3) = target_prob)
    bisection (ikiye bölme) yöntemiyle toplam gol beklentisini (lambda) çözer.
    """
    target_prob = max(0.02, min(0.96, target_prob))
    low, high = 0.2, 7.5
    for _ in range(32):
        mid = (low + high) / 2.0
        if poisson_over_prob(threshold, mid) < target_prob:
            low = mid
        else:
            high = mid
    return (low + high) / 2.0


def strip_vig(odds_list: List[Optional[float]]) -> Tuple[List[float], float]:
    """
    Bahis oranlarındaki kar marjını (overround / vig) arındırarak
    gerçek (adil) olasılıkları ve piyasa marjını döndürür.
    """
    valid = [o if (o and o > 1.0) else None for o in odds_list]
    raw_probs = [1.0 / o if o else 0.0 for o in valid]
    total_implied = sum(raw_probs)
    if total_implied <= 0:
        return [0.0 for _ in odds_list], 0.0
    fair_probs = [p / total_implied for p in raw_probs]
    margin = total_implied - 1.0
    return fair_probs, margin


def dixon_coles_score_matrix(lam_h: float, lam_a: float, rho: float = -0.06, max_goals: int = 8) -> List[List[float]]:
    """
    Dixon-Coles (1997) düzeltmeli 9x9 skor olasılık matrisi.
    0-0, 1-0, 0-1 ve 1-1 skorlarındaki bağımlılığı (rho) hesaba katar.
    """
    matrix = [[0.0] * (max_goals + 1) for _ in range(max_goals + 1)]
    total = 0.0
    for i in range(max_goals + 1):
        p_h = poisson_pmf(i, lam_h)
        for j in range(max_goals + 1):
            p_a = poisson_pmf(j, lam_a)
            tau = 1.0
            if i == 0 and j == 0:
                tau = 1.0 - (lam_h * lam_a * rho)
            elif i == 0 and j == 1:
                tau = 1.0 + (lam_h * rho)
            elif i == 1 and j == 0:
                tau = 1.0 + (lam_a * rho)
            elif i == 1 and j == 1:
                tau = 1.0 - rho
            tau = max(0.1, tau)
            val = p_h * p_a * tau
            matrix[i][j] = val
            total += val

    if total > 0:
        for i in range(max_goals + 1):
            for j in range(max_goals + 1):
                matrix[i][j] /= total
    return matrix


class MatchQuantitativeAnalyzer:
    """
    Tek bir maçı tüm global bahis modelleriyle analiz eder,
    beklenen golleri (xG / Lambda), skor matrisini ve 10 farklı taktik sinyalini üretir.
    """

    STRATEGY_METADATA = {
        "kg_var": {
            "title": "🎯 KG Var (BTTS Sniper) Taktiği",
            "short": "KG Var",
            "desc": "Her iki takımın da bireysel gol beklentisinin (λ_ev ≥ 1.15, λ_dep ≥ 1.05) yüksek olduğu ve savunma zaafiyeti sinyali veren dengeli hücum maçları.",
        },
        "ms_banko": {
            "title": "🏆 MS (Maç Sonucu) Favori & Sürpriz Value Taktiği",
            "short": "MS 1/2",
            "desc": "Takımlar arası güç asimetrisinin (Δλ) yüksek olduğu, çoklu market (MS + Ev/Dep 1.5 Üst + Handikap) onaylı maç sonucu tahminleri.",
        },
        "ust_25": {
            "title": "🔥 2.5 Üst Gol Taktiği",
            "short": "2.5 Üst",
            "desc": "Bivariate Poisson modelinde toplam gol beklentisi λ_toplam ≥ 2.75 olan ve İY 0.5/1.5 Üst marketleriyle çapraz doğrulanan maçlar.",
        },
        "ust_35": {
            "title": "🚀 3.5 Üst Gol Fırtınası Taktiği",
            "short": "3.5 Üst",
            "desc": "Toplam gol beklentisi λ_toplam ≥ 3.15 olan, açık futbol ve erken gol (İY 1.5 Üst) sinyali taşıyan yüksek tempolu maçlar.",
        },
        "arti_6_gol": {
            "title": "💣 +6 Gol (6+ Gol) Ultra Varyans / Kaos Taktiği",
            "short": "+6 Gol",
            "desc": "Poisson sağ-kuyruk (Fat-Tail) dağılımında λ_toplam ≥ 3.35 olan, 3.5/4.5 Üst oranları sert baskılanmış yüksek varyanslı sürpriz maçlar.",
        },
        "iki_yari_kg": {
            "title": "⚡ İki Yarıda da KG Var (Evet/Evet) Taktiği",
            "short": "İki Yarı KG Var",
            "desc": "Simetrik yüksek tempo (λ_ev ≥ 1.35 ve λ_dep ≥ 1.25) + İlk Yarı KG Var ve İkinci Yarı KG Var olasılıklarının kesiştiği yüksek oranlı taktik.",
        },
        "iy_ust": {
            "title": "⏱️ İlk Yarı 1.5 Üst / Erken Baskı (Blitz) Taktiği",
            "short": "İY 1.5 Üst",
            "desc": "İlk 45 dakika gol yoğunluğu (λ_1Y ≥ 1.30) yüksek olan, maça hızlı başlayan takımların erken gol taktiği.",
        },
        "kombo": {
            "title": "💎 Kombo (KG Var & 2.5 Üst / MS & 2.5 Üst) Taktiği",
            "short": "Kombo Bahis",
            "desc": "Tekli oranı düşük kalan maçlarda çapraz korelasyon kullanarak olasılık kaybını minimumda tutup oranı 1.75 - 2.60 bandına çıkaran taktik.",
        },
        "korner": {
            "title": "📐 Korner 9.5 / 10.5 Üst Kanat Baskısı Taktiği",
            "short": "Korner Üst",
            "desc": "Hücum temposu ve şut/kanat baskısı yüksek olup korner çizgisi (9.5 / 10.5 Üst) değerli fiyatlanan maçlar.",
        },
        "value_bet": {
            "title": "🧠 Global Sharp Value Bet (+EV / Oran Sapması) Taktiği",
            "short": "Value Bet (+EV)",
            "desc": "Dixon-Coles model olasılığının bahis şirketinin ima ettiği olasılıktan anlamlı derecede yüksek olduğu (+EV ≥ %5) matematiksel fırsatlar.",
        },
        "alt_taktigi": {
            "title": "🛡️ 2.5 Alt & Kısır Maç (Savunma Duvarı) Taktiği",
            "short": "2.5 Alt / Kısır",
            "desc": "Toplam gol beklentisi λ_toplam ≤ 2.25 olan, savunma disiplini yüksek ve Dixon-Coles 0-0, 1-0, 0-1, 1-1 olasılığı %56+ olan düşük tempolu maçlar.",
        },
        "takim_iki_yari_gol": {
            "title": "🏠 Ev/Dep Her İki Yarıda da Gol Atar Taktiği",
            "short": "İki Yarıda Gol Atar",
            "desc": "Ağır favori takımın hücum gücünün (λ_takım ≥ 1.85) maçın iki devresine de yayıldığı, hem 1. Yarı hem 2. Yarı gol atma olasılığı yüksek olan maçlar.",
        },
        "kart_korner": {
            "title": "🟨 Kart & Korner Baskı Radarı",
            "short": "Kart & Korner",
            "desc": "Tansiyonu, derbi/rekabet seviyesi ve kanat baskısı yüksek maçlarda Kart 3.5/4.5 Üst, Kırmızı Kart ve Korner 9.5 Üst/Alt tercihleri.",
        },
        "iyms_surpriz": {
            "title": "🔄 İY/MS Geri Dönüşler (1/2 - 2/1) & İkinci Yarıda Çözenler (X/1 - X/2)",
            "short": "İY/MS Sürpriz",
            "desc": "Efsane geri dönüş potansiyeli taşıyan (1/2, 2/1 — Oran 20.00-35.00) ve ilk yarı temkinli geçip 2. yarıda favorinin açtığı (X/1, X/2 — Oran 4.50-6.50) maçlar.",
        },
        "kesin_skor": {
            "title": "🎯 Dixon-Coles Kesin Skor (Correct Score) Sniper",
            "short": "Kesin Skor",
            "desc": "Dixon-Coles 9x9 matrisinde gerçek olasılığı en yüksek ve piyasa oranına göre en değerli kesin skor tahminleri (1-1, 2-1, 1-2, 2-0, 2-2, 3-1).",
        },
        "tg_aralik": {
            "title": "⚖️ Toplam Gol Aralığı (2-3 Gol İdeal & 4-5 Gol Yüksek Bant)",
            "short": "Toplam Gol Aralığı",
            "desc": "Aşırı uç maçlar yerine tam 2-3 Gol (Oran 1.75-1.95) ve 4-5 Gol (Oran 2.80-3.60) bandına oturan dengeli kupon tamamlayıcıları.",
        },
    }

    def analyze_match(self, match: Dict[str, Any], espn_details: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        odds = match.get("odds", {})

        # 1. MS 1/X/2 Vig-Free Olasılıkları
        p_ms, ms_margin = strip_vig([odds.get("ms_1"), odds.get("ms_x"), odds.get("ms_2")])
        p_home_mkt, p_draw_mkt, p_away_mkt = p_ms[0], p_ms[1], p_ms[2]

        # 2. Alt/Üst Marketlerinden Toplam Gol Beklentisi (Lambda Total) Çıkarma
        lam_estimates: List[Tuple[float, float]] = []  # (lambda, weight)

        if odds.get("ust_25") and odds.get("alt_25"):
            p_25, _ = strip_vig([odds.get("ust_25"), odds.get("alt_25")])
            lam_estimates.append((solve_lambda_from_over_prob(p_25[0], 2.5), 3.0))
        elif odds.get("ust_25"):
            p_approx = min(0.88, (1.0 / odds["ust_25"]) * 0.93)
            lam_estimates.append((solve_lambda_from_over_prob(p_approx, 2.5), 1.5))

        if odds.get("ust_35") and odds.get("alt_35"):
            p_35, _ = strip_vig([odds.get("ust_35"), odds.get("alt_35")])
            lam_estimates.append((solve_lambda_from_over_prob(p_35[0], 3.5), 2.2))

        if odds.get("ust_15") and odds.get("alt_15"):
            p_15, _ = strip_vig([odds.get("ust_15"), odds.get("alt_15")])
            lam_estimates.append((solve_lambda_from_over_prob(p_15[0], 1.5), 1.5))

        if lam_estimates:
            lam_total = sum(v * w for v, w in lam_estimates) / sum(w for _, w in lam_estimates)
        else:
            lam_total = 2.55

        # 3. Takım Bazlı Gol Beklentisi (Lambda Home & Lambda Away) Ayrıştırması
        # Ev/Dep 1.5 Üst marketleri varsa doğrudan takım lambdasını kalibre et
        lam_h_direct, lam_a_direct = None, None
        if odds.get("ev_ust_15") and odds.get("ev_alt_15"):
            p_ev15, _ = strip_vig([odds.get("ev_ust_15"), odds.get("ev_alt_15")])
            lam_h_direct = solve_lambda_from_over_prob(p_ev15[0], 1.5)
        if odds.get("dep_ust_15") and odds.get("dep_alt_15"):
            p_dep15, _ = strip_vig([odds.get("dep_ust_15"), odds.get("dep_alt_15")])
            lam_a_direct = solve_lambda_from_over_prob(p_dep15[0], 1.5)

        # MS olasılıklarından güç oranı
        non_draw = max(0.10, p_home_mkt + p_away_mkt)
        home_share = (p_home_mkt / non_draw) * 0.78 + 0.50 * 0.22
        home_share = max(0.18, min(0.82, home_share))

        lam_h = lam_total * home_share
        lam_a = lam_total * (1.0 - home_share)

        if lam_h_direct is not None and lam_a_direct is not None:
            lam_h = 0.55 * lam_h + 0.45 * lam_h_direct
            lam_a = 0.55 * lam_a + 0.45 * lam_a_direct
            lam_total = lam_h + lam_a

        # KG Var / Yok marketi ile simetri düzeltmesi
        p_btts_mkt = 0.0
        if odds.get("kg_var") and odds.get("kg_yok"):
            p_kg, _ = strip_vig([odds.get("kg_var"), odds.get("kg_yok")])
            p_btts_mkt = p_kg[0]
            # Eğer KG Var olasılığı yüksekse zayıf takımın gol beklentisini yukarı çek
            poisson_btts = (1.0 - math.exp(-lam_h)) * (1.0 - math.exp(-lam_a))
            if p_btts_mkt > poisson_btts + 0.03:
                boost = min(0.22, (p_btts_mkt - poisson_btts) * 0.85)
                if lam_h < lam_a:
                    lam_h += boost
                else:
                    lam_a += boost
                lam_total = lam_h + lam_a

        # 4. ESPN / Son 5 Maç Formu & H2H Entegrasyonu (Varsa)
        form_notes: List[str] = []
        if espn_details:
            h_gf = espn_details.get("home_last5_goals_for")
            h_ga = espn_details.get("home_last5_goals_against")
            a_gf = espn_details.get("away_last5_goals_for")
            a_ga = espn_details.get("away_last5_goals_against")
            if h_gf is not None and a_ga is not None:
                form_lam_h = (h_gf + a_ga) / 2.0
                lam_h = 0.80 * lam_h + 0.20 * form_lam_h
                form_notes.append(f"Ev Son 5 Maç Gol Ort: {h_gf:.1f} Attı / {h_ga:.1f} Yedi")
            if a_gf is not None and h_ga is not None:
                form_lam_a = (a_gf + h_ga) / 2.0
                lam_a = 0.80 * lam_a + 0.20 * form_lam_a
                form_notes.append(f"Dep Son 5 Maç Gol Ort: {a_gf:.1f} Attı / {a_ga:.1f} Yedi")
            if espn_details.get("h2h_avg_goals") is not None:
                h2h_g = espn_details["h2h_avg_goals"]
                form_notes.append(f"H2H Gol Ort: {h2h_g:.1f}")
            lam_total = lam_h + lam_a

        # 5. Dixon-Coles 9x9 Skor Matrisi Hesabı
        score_matrix = dixon_coles_score_matrix(lam_h, lam_a, rho=-0.05)

        p_home_dc = 0.0
        p_draw_dc = 0.0
        p_away_dc = 0.0
        p_btts_dc = 0.0
        p_over15_dc = 0.0
        p_over25_dc = 0.0
        p_over35_dc = 0.0
        p_over45_dc = 0.0
        p_6plus_dc = 0.0
        p_ms1_over25_dc = 0.0
        p_ms2_over25_dc = 0.0
        p_btts_over25_dc = 0.0

        exact_scores: List[Tuple[str, float]] = []

        for i in range(9):
            for j in range(9):
                p = score_matrix[i][j]
                if i <= 5 and j <= 5:
                    exact_scores.append((f"{i}-{j}", p))
                if i > j:
                    p_home_dc += p
                    if (i + j) >= 3:
                        p_ms1_over25_dc += p
                elif i == j:
                    p_draw_dc += p
                else:
                    p_away_dc += p
                    if (i + j) >= 3:
                        p_ms2_over25_dc += p

                if i >= 1 and j >= 1:
                    p_btts_dc += p
                    if (i + j) >= 3:
                        p_btts_over25_dc += p

                tot = i + j
                if tot >= 2:
                    p_over15_dc += p
                if tot >= 3:
                    p_over25_dc += p
                if tot >= 4:
                    p_over35_dc += p
                if tot >= 5:
                    p_over45_dc += p
                if tot >= 6:
                    p_6plus_dc += p

        exact_scores.sort(key=lambda x: x[1], reverse=True)
        top_3_scores = exact_scores[:3]

        # 6. İlk Yarı / İkinci Yarı Tempo & İki Yarı KG Var Modeli
        # Global futbol istatistiği: Gollerin ortalama %44'ü 1. yarıda, %56'sı 2. yarıda atılır.
        lam_1h_total = lam_total * 0.44
        if odds.get("iy_ust_15") and odds.get("iy_alt_15"):
            p_iy15_mkt, _ = strip_vig([odds.get("iy_ust_15"), odds.get("iy_alt_15")])
            lam_1h_mkt = solve_lambda_from_over_prob(p_iy15_mkt[0], 1.5)
            lam_1h_total = 0.55 * lam_1h_total + 0.45 * lam_1h_mkt

        lam_2h_total = max(0.5, lam_total - lam_1h_total)
        h_ratio = lam_h / max(0.1, lam_total)
        a_ratio = lam_a / max(0.1, lam_total)

        lam_1h_h = lam_1h_total * h_ratio
        lam_1h_a = lam_1h_total * a_ratio
        lam_2h_h = lam_2h_total * h_ratio
        lam_2h_a = lam_2h_total * a_ratio

        p_iy_over05 = 1.0 - math.exp(-lam_1h_total)
        p_iy_over15 = poisson_over_prob(1.5, lam_1h_total)
        p_1h_btts = (1.0 - math.exp(-lam_1h_h)) * (1.0 - math.exp(-lam_1h_a))
        p_2h_btts = (1.0 - math.exp(-lam_2h_h)) * (1.0 - math.exp(-lam_2h_a))

        # Market kalibrasyonu (İlk Yarı KG ve 2. Yarı KG oranları varsa harmanla)
        if odds.get("iy_kg_var") and odds.get("iy_kg_yok"):
            p_iykg_m, _ = strip_vig([odds.get("iy_kg_var"), odds.get("iy_kg_yok")])
            p_1h_btts = 0.60 * p_1h_btts + 0.40 * p_iykg_m[0]
        if odds.get("ikinci_yari_kg_var") and odds.get("ikinci_yari_kg_yok"):
            p_2ykg_m, _ = strip_vig([odds.get("ikinci_yari_kg_var"), odds.get("ikinci_yari_kg_yok")])
            p_2h_btts = 0.60 * p_2h_btts + 0.40 * p_2ykg_m[0]

        # İki yarıda da KG Var (1Y Evet & 2Y Evet) - pozitif tempo korelasyonu (1.08x)
        p_both_halves_btts = min(0.35, p_1h_btts * p_2h_btts * 1.08)

        # Kaos / Varyans Endeksi (+6 Gol ve yüksek skor sürprizleri için)
        # Hem toplam lambda yüksek hem de iki takım da skora katkı veriyorsa varyans artar
        balance_factor = 1.0 - abs(h_ratio - a_ratio)  # 0..1 arası (1 = tam dengeli)
        chaos_index = (lam_total / 3.0) * 55.0 + (p_btts_dc * 30.0) + (p_iy_over15 * 25.0)
        chaos_index = min(99.0, max(10.0, chaos_index))

        # 7. Strateji Sinyalleri ve Nedenlerini Üret
        signals = self._evaluate_all_strategies(
            match=match,
            odds=odds,
            lam_h=lam_h,
            lam_a=lam_a,
            lam_total=lam_total,
            lam_1h_total=lam_1h_total,
            p_home=p_home_dc,
            p_draw=p_draw_dc,
            p_away=p_away_dc,
            p_btts=p_btts_dc,
            p_over25=p_over25_dc,
            p_over35=p_over35_dc,
            p_6plus=p_6plus_dc,
            p_iy_over05=p_iy_over05,
            p_iy_over15=p_iy_over15,
            p_1h_btts=p_1h_btts,
            p_2h_btts=p_2h_btts,
            p_both_halves_btts=p_both_halves_btts,
            p_ms1_over25=p_ms1_over25_dc,
            p_ms2_over25=p_ms2_over25_dc,
            p_btts_over25=p_btts_over25_dc,
            chaos_index=chaos_index,
            balance_factor=balance_factor,
            top_3_scores=top_3_scores,
            form_notes=form_notes,
            exact_scores=exact_scores,
        )

        return {
            "match": match,
            "lam_home": round(lam_h, 2),
            "lam_away": round(lam_a, 2),
            "lam_total": round(lam_total, 2),
            "lam_1h": round(lam_1h_total, 2),
            "chaos_index": round(chaos_index, 1),
            "probabilities": {
                "ms_1": round(p_home_dc * 100, 1),
                "ms_x": round(p_draw_dc * 100, 1),
                "ms_2": round(p_away_dc * 100, 1),
                "kg_var": round(p_btts_dc * 100, 1),
                "ust_25": round(p_over25_dc * 100, 1),
                "ust_35": round(p_over35_dc * 100, 1),
                "arti_6_gol": round(p_6plus_dc * 100, 1),
                "iy_ust_15": round(p_iy_over15 * 100, 1),
                "iy_kg_var": round(p_1h_btts * 100, 1),
                "ikinci_yari_kg_var": round(p_2h_btts * 100, 1),
                "iki_yari_kg": round(p_both_halves_btts * 100, 1),
            },
            "top_scores": [(s, round(p * 100, 1)) for s, p in top_3_scores],
            "form_notes": form_notes,
            "signals": signals,
        }

    def _evaluate_all_strategies(
        self,
        match: Dict[str, Any],
        odds: Dict[str, float],
        lam_h: float,
        lam_a: float,
        lam_total: float,
        lam_1h_total: float,
        p_home: float,
        p_draw: float,
        p_away: float,
        p_btts: float,
        p_over25: float,
        p_over35: float,
        p_6plus: float,
        p_iy_over05: float,
        p_iy_over15: float,
        p_1h_btts: float,
        p_2h_btts: float,
        p_both_halves_btts: float,
        p_ms1_over25: float,
        p_ms2_over25: float,
        p_btts_over25: float,
        chaos_index: float,
        balance_factor: float,
        top_3_scores: List[Tuple[str, float]],
        form_notes: List[str],
        exact_scores: Optional[List[Tuple[str, float]]] = None,
    ) -> Dict[str, Dict[str, Any]]:
        signals: Dict[str, Dict[str, Any]] = {}

        # ------------------------------------------------------------------
        # 1. KG VAR (BTTS Sniper) TAKTİĞİ
        # ------------------------------------------------------------------
        kg_odd = odds.get("kg_var")
        if kg_odd and kg_odd >= 1.30 and lam_h >= 1.08 and lam_a >= 0.98 and p_btts >= 0.56:
            ev = (p_btts * kg_odd) - 1.0
            # Zayıf tarafın gol atma olasılığı (1 - e^-lam_min)
            min_lam = min(lam_h, lam_a)
            weak_score_prob = (1.0 - math.exp(-min_lam)) * 100
            score = (p_btts * 100) * 0.65 + weak_score_prob * 0.35 + max(0.0, ev * 35)
            reasons = [
                f"Çift yönlü hücum beklentisi yüksek: Ev xG={lam_h:.2f}, Dep xG={lam_a:.2f}",
                f"Zayıf tarafın en az 1 gol bulma olasılığı %{weak_score_prob:.0f}, Dixon-Coles KG Var olasılığı %{p_btts*100:.1f}",
            ]
            if odds.get("ust_25") and odds["ust_25"] <= 1.65:
                reasons.append(f"2.5 Üst oranı ({odds['ust_25']:.2f}) gollü maç senaryosunu çapraz doğruluyor")
            if form_notes:
                reasons.append(" | ".join(form_notes[:2]))
            signals["kg_var"] = {
                "pick": "KG Var (Karşılıklı Gol Var)",
                "odd": kg_odd,
                "prob": round(p_btts * 100, 1),
                "confidence": round(min(98.0, score), 1),
                "ev_pct": round(ev * 100, 1),
                "reasons": reasons,
            }

        # ------------------------------------------------------------------
        # 2. MS (MAÇ SONUCU) BANKO & DEĞERLİ FAVORİ TAKTİĞİ
        # ------------------------------------------------------------------
        best_ms_side = "1" if p_home >= p_away else "2"
        best_ms_prob = p_home if best_ms_side == "1" else p_away
        best_ms_odd = odds.get("ms_1") if best_ms_side == "1" else odds.get("ms_2")
        lam_diff = abs(lam_h - lam_a)

        if best_ms_odd and 1.28 <= best_ms_odd <= 2.35 and best_ms_prob >= 0.53 and lam_diff >= 0.55:
            ev = (best_ms_prob * best_ms_odd) - 1.0
            team_name = match["home"] if best_ms_side == "1" else match["away"]
            score = (best_ms_prob * 100) * 0.75 + (lam_diff * 15.0) + max(0.0, ev * 40)
            reasons = [
                f"Güç asimetrisi (Δλ={lam_diff:.2f}): {team_name} lehine gol beklentisi {max(lam_h, lam_a):.2f} vs {min(lam_h, lam_a):.2f}",
                f"Dixon-Coles kazanma olasılığı %{best_ms_prob*100:.1f} (En olası skorlar: {', '.join(s for s, _ in top_3_scores[:2])})",
            ]
            team_15_odd = odds.get("ev_ust_15") if best_ms_side == "1" else odds.get("dep_ust_15")
            if team_15_odd and team_15_odd <= 1.75:
                reasons.append(f"{team_name} 1.5 Gol Üst oranı ({team_15_odd:.2f}) favorinin skor üretimini destekliyor")
            signals["ms_banko"] = {
                "pick": f"MS {best_ms_side} ({team_name} Kazanır)",
                "odd": best_ms_odd,
                "prob": round(best_ms_prob * 100, 1),
                "confidence": round(min(98.0, score), 1),
                "ev_pct": round(ev * 100, 1),
                "reasons": reasons,
            }

        # ------------------------------------------------------------------
        # 3. 2.5 ÜST GOL TAKTİĞİ
        # ------------------------------------------------------------------
        u25_odd = odds.get("ust_25")
        if u25_odd and u25_odd >= 1.32 and lam_total >= 2.75 and p_over25 >= 0.56:
            ev = (p_over25 * u25_odd) - 1.0
            score = (p_over25 * 100) * 0.72 + (lam_total * 7.5) + max(0.0, ev * 35)
            reasons = [
                f"Toplam beklenen gol λ_toplam={lam_total:.2f} (Ev {lam_h:.2f} + Dep {lam_a:.2f}) 2.5 eşiğinin net üzerinde",
                f"Bivariate Poisson 3+ gol olasılığı %{p_over25*100:.1f} ve İY en az 1 gol olasılığı %{p_iy_over05*100:.1f}",
            ]
            if odds.get("iy_ust_15") and odds["iy_ust_15"] <= 2.30:
                reasons.append(f"İlk Yarı 1.5 Üst oranı ({odds['iy_ust_15']:.2f}) maçın erken açılacağını işaret ediyor")
            signals["ust_25"] = {
                "pick": "2.5 Gol Üst",
                "odd": u25_odd,
                "prob": round(p_over25 * 100, 1),
                "confidence": round(min(98.0, score), 1),
                "ev_pct": round(ev * 100, 1),
                "reasons": reasons,
            }

        # ------------------------------------------------------------------
        # 4. 3.5 ÜST GOL FIRTINASI TAKTİĞİ
        # ------------------------------------------------------------------
        u35_odd = odds.get("ust_35")
        if u35_odd and u35_odd >= 1.65 and lam_total >= 3.10 and p_over35 >= 0.37:
            ev = (p_over35 * u35_odd) - 1.0
            score = (p_over35 * 100) * 0.85 + (lam_total * 9.0) + max(0.0, ev * 35)
            reasons = [
                f"Yüksek gol beklentisi: λ_toplam={lam_total:.2f} ile 4+ gol olasılığı %{p_over35*100:.1f}",
                f"Kaos/Varyans Endeksi {chaos_index:.0f}/100 — savunma dengesi düşük, açık tempo maçı",
            ]
            signals["ust_35"] = {
                "pick": "3.5 Gol Üst",
                "odd": u35_odd,
                "prob": round(p_over35 * 100, 1),
                "confidence": round(min(96.0, score), 1),
                "ev_pct": round(ev * 100, 1),
                "reasons": reasons,
            }

        # ------------------------------------------------------------------
        # 5. +6 GOL (6+ GOL) ULTRA VARYANS / KAOS TAKTİĞİ
        # ------------------------------------------------------------------
        g6_odd = odds.get("arti_6_gol") or odds.get("ust_45")
        if g6_odd and lam_total >= 3.20 and p_6plus >= 0.095:
            pick_label = "Toplam Gol 6+ (+6 Gol)" if odds.get("arti_6_gol") else "4.5 Gol Üst (Yüksek Skor)"
            ev = (p_6plus * g6_odd) - 1.0 if odds.get("arti_6_gol") else 0.0
            score = (p_6plus * 100) * 3.2 + chaos_index * 0.45 + max(0.0, ev * 25)
            reasons = [
                f"Poisson Sağ-Kuyruk (Fat-Tail) Analizi: λ_toplam={lam_total:.2f} ile 6+ gol olasılığı %{p_6plus*100:.1f} (Normal lig ortalamasının ~4 katı!)",
                f"Kaos & Varyans Endeksi: {chaos_index:.1f}/100 | İlk yarı 1.5 Üst olasılığı %{p_iy_over15*100:.1f}",
                f"Hem ağır hücum baskısı (Max λ={max(lam_h, lam_a):.2f}) hem karşı gol (Min λ={min(lam_h, lam_a):.2f}) mevcut (4-2, 5-1, 3-3 potansiyeli)",
            ]
            signals["arti_6_gol"] = {
                "pick": pick_label,
                "odd": g6_odd,
                "prob": round(p_6plus * 100, 1),
                "confidence": round(min(95.0, score), 1),
                "ev_pct": round(ev * 100, 1),
                "reasons": reasons,
            }

        # ------------------------------------------------------------------
        # 6. İKİ YARIDA DA KG VAR (1Y EVET & 2Y EVET) TAKTİĞİ
        # ------------------------------------------------------------------
        # Bu taktik için tek taraflı 5-0 maç değil, DENGELİ YÜKSEK TEMPO (λ_ev >= 1.25 ve λ_dep >= 1.15) gerekir!
        both_kg_odd = odds.get("iki_yari_kg_var")  # Sadece bültende AÇIK olan gerçek oran

        if both_kg_odd and lam_h >= 1.25 and lam_a >= 1.15 and p_both_halves_btts >= 0.065:
            display_odd = both_kg_odd
            ev = (p_both_halves_btts * display_odd) - 1.0
            score = (p_both_halves_btts * 100) * 4.2 + (balance_factor * 25.0) + (p_1h_btts * 100 * 0.8)
            reasons = [
                f"Simetrik Yüksek Tempo: Ev λ={lam_h:.2f} & Dep λ={lam_a:.2f} (Denge Skoru %{balance_factor*100:.0f})",
                f"1. Yarı KG Var olasılığı %{p_1h_btts*100:.1f} (Oran: {odds.get('iy_kg_var', '-')}) ve 2. Yarı KG Var olasılığı %{p_2h_btts*100:.1f} (Oran: {odds.get('ikinci_yari_kg_var', '-')})",
                f"Birleşik İki Yarıda da KG Var (Evet/Evet) olasılığı %{p_both_halves_btts*100:.1f} (Alt kademe kuponlar için: İY KG Var veya 2Y KG Var tekli de değerlendirilebilir)",
            ]
            signals["iki_yari_kg"] = {
                "pick": f"İki Yarıda da KG Var (1Y KG: {odds.get('iy_kg_var', '-')} / 2Y KG: {odds.get('ikinci_yari_kg_var', '-')})",
                "odd": display_odd,
                "prob": round(p_both_halves_btts * 100, 1),
                "confidence": round(min(94.0, score), 1),
                "ev_pct": round(ev * 100, 1),
                "reasons": reasons,
            }

        # ------------------------------------------------------------------
        # 7. İLK YARI 1.5 ÜST / ERKEN BASKI (BLITZ) TAKTİĞİ
        # ------------------------------------------------------------------
        iy15_odd = odds.get("iy_ust_15")
        if iy15_odd and iy15_odd >= 1.75 and lam_1h_total >= 1.25 and p_iy_over15 >= 0.36:
            ev = (p_iy_over15 * iy15_odd) - 1.0
            score = (p_iy_over15 * 100) * 1.15 + (lam_1h_total * 18.0) + max(0.0, ev * 35)
            reasons = [
                f"İlk 45 dakika gol beklentisi λ_1Y={lam_1h_total:.2f} (İY en az 1 gol: %{p_iy_over05*100:.1f}, İY 2+ gol: %{p_iy_over15*100:.1f})",
                f"İY 1.5 Üst oranı ({iy15_odd:.2f}) maç sonu temposuna göre yüksek erken gol potansiyeli taşıyor",
            ]
            signals["iy_ust"] = {
                "pick": "İlk Yarı 1.5 Gol Üst",
                "odd": iy15_odd,
                "prob": round(p_iy_over15 * 100, 1),
                "confidence": round(min(95.0, score), 1),
                "ev_pct": round(ev * 100, 1),
                "reasons": reasons,
            }

        # ------------------------------------------------------------------
        # 8. KOMBO (KG VAR & 2.5 ÜST / MS & 2.5 ÜST) TAKTİĞİ
        # ------------------------------------------------------------------
        combo_candidates = []
        if odds.get("kg_var_ve_25_ust") and p_btts_over25 >= 0.45:
            c_odd = odds["kg_var_ve_25_ust"]
            c_ev = (p_btts_over25 * c_odd) - 1.0
            combo_candidates.append((
                "KG Var & 2.5 Üst",
                c_odd,
                p_btts_over25,
                c_ev,
                f"KG Var (%{p_btts*100:.0f}) ve 2.5 Üst (%{p_over25*100:.0f}) yüksek pozitif korelasyonlu; tekli oran yerine kombo oran ({c_odd:.2f}) değerli",
            ))
        if odds.get("ms1_ve_25_ust") and p_ms1_over25 >= 0.42:
            c_odd = odds["ms1_ve_25_ust"]
            c_ev = (p_ms1_over25 * c_odd) - 1.0
            combo_candidates.append((
                f"MS 1 & 2.5 Üst ({match['home']})",
                c_odd,
                p_ms1_over25,
                c_ev,
                f"Ev sahibi galibiyeti (%{p_home*100:.0f}) ve 2.5 Üst (%{p_over25*100:.0f}) kesişimi %{p_ms1_over25*100:.1f}",
            ))
        if odds.get("ms2_ve_25_ust") and p_ms2_over25 >= 0.42:
            c_odd = odds["ms2_ve_25_ust"]
            c_ev = (p_ms2_over25 * c_odd) - 1.0
            combo_candidates.append((
                f"MS 2 & 2.5 Üst ({match['away']})",
                c_odd,
                p_ms2_over25,
                c_ev,
                f"Deplasman galibiyeti (%{p_away*100:.0f}) ve 2.5 Üst (%{p_over25*100:.0f}) kesişimi %{p_ms2_over25*100:.1f}",
            ))

        if combo_candidates:
            combo_candidates.sort(key=lambda x: (x[2] * 0.7 + max(0.0, x[3]) * 0.3), reverse=True)
            best_c = combo_candidates[0]
            signals["kombo"] = {
                "pick": best_c[0],
                "odd": best_c[1],
                "prob": round(best_c[2] * 100, 1),
                "confidence": round(min(95.0, best_c[2] * 100 * 1.15 + max(0.0, best_c[3] * 30)), 1),
                "ev_pct": round(best_c[3] * 100, 1),
                "reasons": [
                    best_c[4],
                    f"Toplam gol beklentisi λ={lam_total:.2f} (En olası skorlar: {', '.join(s for s, _ in top_3_scores)})",
                ],
            }

        # ------------------------------------------------------------------
        # 9. KORNER 9.5 / 10.5 ÜST KANAT BASKISI TAKTİĞİ
        # ------------------------------------------------------------------
        k95_odd = odds.get("korner_95_ust")
        if k95_odd and lam_total >= 2.65:
            p_k95 = 0.50
            if odds.get("korner_95_alt"):
                p_k, _ = strip_vig([k95_odd, odds.get("korner_95_alt")])
                p_k95 = p_k[0]
            # Yüksek gol beklentisi ve dengeli maçlarda şut/korner korelasyonu artar
            adj_k_prob = min(0.75, p_k95 + (lam_total - 2.60) * 0.06 + balance_factor * 0.04)
            if adj_k_prob >= 0.52:
                ev = (adj_k_prob * k95_odd) - 1.0
                signals["korner"] = {
                    "pick": "Toplam Korner 9.5 Üst",
                    "odd": k95_odd,
                    "prob": round(adj_k_prob * 100, 1),
                    "confidence": round(min(93.0, adj_k_prob * 100 * 1.1 + lam_total * 5), 1),
                    "ev_pct": round(ev * 100, 1),
                    "reasons": [
                        f"Yüksek hücum temposu (λ={lam_total:.2f}) ve kanat/şut baskısı korner ortalamasını yukarı çekiyor",
                        f"9.5 Üst Korner model olasılığı %{adj_k_prob*100:.1f} (10.5 Üst Oranı: {odds.get('korner_105_ust', '-')})",
                    ],
                }

        # ------------------------------------------------------------------
        # 10. GLOBAL SHARP VALUE BET (+EV / ÇAPRAZ MARKET ORAN SAPMASI & KELLY) TAKTİĞİ
        # ------------------------------------------------------------------
        # Çapraz marketlerden (15+ alt market) hesaplanan Dixon-Coles olasılığı ile
        # tekil marketin marjdan arındırılmış (vig-free) olasılığı arasındaki sapmayı yakalar.
        p_ms_fair, _ = strip_vig([odds.get("ms_1"), odds.get("ms_x"), odds.get("ms_2")])
        p_kg_fair, _ = strip_vig([odds.get("kg_var"), odds.get("kg_yok")]) if odds.get("kg_var") and odds.get("kg_yok") else ([0.0, 0.0], 0.0)
        p_25_fair, _ = strip_vig([odds.get("ust_25"), odds.get("alt_25")]) if odds.get("ust_25") and odds.get("alt_25") else ([0.0, 0.0], 0.0)
        p_35_fair, _ = strip_vig([odds.get("ust_35"), odds.get("alt_35")]) if odds.get("ust_35") and odds.get("alt_35") else ([0.0, 0.0], 0.0)
        p_iy15_fair, _ = strip_vig([odds.get("iy_ust_15"), odds.get("iy_alt_15")]) if odds.get("iy_ust_15") and odds.get("iy_alt_15") else ([0.0, 0.0], 0.0)

        value_checks = [
            ("MS 1", odds.get("ms_1"), p_home, p_ms_fair[0]),
            ("MS 2", odds.get("ms_2"), p_away, p_ms_fair[2]),
            ("KG Var", odds.get("kg_var"), p_btts, p_kg_fair[0]),
            ("2.5 Üst", odds.get("ust_25"), p_over25, p_25_fair[0]),
            ("3.5 Üst", odds.get("ust_35"), p_over35, p_35_fair[0]),
            ("İY 1.5 Üst", odds.get("iy_ust_15"), p_iy_over15, p_iy15_fair[0]),
        ]
        best_val = None
        for label, odd_v, prob_model, prob_mkt in value_checks:
            if not odd_v or odd_v < 1.45 or odd_v > 4.20 or prob_model < 0.38 or prob_mkt <= 0.05:
                continue
            # Çapraz market göreli değer farkı (Relative Edge)
            rel_edge = (prob_model / prob_mkt) - 1.0
            abs_diff = prob_model - prob_mkt
            if rel_edge >= 0.055 and abs_diff >= 0.028:
                kelly_pct = max(1.0, min(5.0, (rel_edge / max(0.5, odd_v - 1.0)) * 25.0))
                if best_val is None or rel_edge > best_val[4]:
                    best_val = (label, odd_v, prob_model, prob_mkt, rel_edge, kelly_pct)

        if best_val:
            lbl, o_v, pr_mod, pr_mkt, rel_edge, kelly_v = best_val
            signals["value_bet"] = {
                "pick": f"{lbl} (Sharp Value)",
                "odd": o_v,
                "prob": round(pr_mod * 100, 1),
                "confidence": round(min(97.0, 72.0 + rel_edge * 160), 1),
                "ev_pct": round(rel_edge * 100, 1),
                "reasons": [
                    f"Çapraz Market Sapması: Tekil market bu tercihi %{pr_mkt*100:.1f} olarak fiyatlamış, ancak maçın diğer alt marketleri (xG/Dixon-Coles) %{pr_mod*100:.1f} gerçek olasılık veriyor!",
                    f"Matematiksel Değer Avantajı (Edge): +%{rel_edge*100:.1f} | Quarter-Kelly Önerilen Kasa Payı: %{kelly_v:.1f}",
                ],
            }

        # ------------------------------------------------------------------
        # 11. 🛡️ 2.5 ALT & KISIR MAÇ (SAVUNMA DUVARI) TAKTİĞİ
        # ------------------------------------------------------------------
        p_under25 = 1.0 - p_over25
        p_under35 = 1.0 - p_over35
        p_btts_no = 1.0 - p_btts
        if lam_total <= 2.35 and p_under25 >= 0.53:
            alt_pick = "2.5 Gol Alt"
            alt_odd = odds.get("alt_25")
            alt_prob = p_under25
            if not alt_odd and odds.get("alt_35") and p_under35 >= 0.70:
                alt_pick = "3.5 Gol Alt"
                alt_odd = odds.get("alt_35")
                alt_prob = p_under35
            elif not alt_odd and odds.get("kg_yok") and p_btts_no >= 0.55:
                alt_pick = "KG Yok (Karşılıklı Gol Yok)"
                alt_odd = odds.get("kg_yok")
                alt_prob = p_btts_no

            if alt_odd:
                ev = (alt_prob * alt_odd) - 1.0
                score = (alt_prob * 100) * 0.75 + max(0.0, (2.40 - lam_total) * 20.0) + max(0.0, ev * 35)
                signals["alt_taktigi"] = {
                    "pick": alt_pick,
                    "odd": alt_odd,
                    "prob": round(alt_prob * 100, 1),
                    "confidence": round(min(97.0, score), 1),
                    "ev_pct": round(ev * 100, 1),
                    "reasons": [
                        f"Savunma Duvarı Modeli: Toplam beklenen gol λ_toplam={lam_total:.2f} (Ev {lam_h:.2f} + Dep {lam_a:.2f})",
                        f"Dixon-Coles düşük skor olasılığı %{alt_prob*100:.1f} (En olası skorlar: {', '.join(s for s, _ in top_3_scores[:2])})",
                    ],
                }

        # ------------------------------------------------------------------
        # 12. 🏠 EV / DEP TAKIM 1.5 GOL ÜST TAKTİĞİ (doğrulanmış market: MTID 20 / 29)
        # ------------------------------------------------------------------
        p_ev_over15 = poisson_over_prob(1.5, lam_h)
        p_dep_over15 = poisson_over_prob(1.5, lam_a)
        team_cands = []
        if odds.get("ev_ust_15") and lam_h >= 1.75:
            team_cands.append((match["home"], "Ev Sahibi", odds["ev_ust_15"], p_ev_over15, lam_h))
        if odds.get("dep_ust_15") and lam_a >= 1.75:
            team_cands.append((match["away"], "Deplasman", odds["dep_ust_15"], p_dep_over15, lam_a))
        team_cands = [c for c in team_cands if c[3] >= 0.50]
        if team_cands:
            name, side, t_odd, t_prob, t_lam = max(team_cands, key=lambda c: c[3])
            ev = (t_prob * t_odd) - 1.0
            signals["takim_iki_yari_gol"] = {
                "pick": f"{side} ({name}) 1.5 Gol Üst",
                "odd": t_odd,
                "prob": round(t_prob * 100, 1),
                "confidence": round(min(95.0, t_prob * 100 * 1.1 + t_lam * 6), 1),
                "ev_pct": round(ev * 100, 1),
                "reasons": [
                    f"{name} gol beklentisi yüksek (λ={t_lam:.2f}); 2+ gol atma model olasılığı %{t_prob*100:.1f}",
                ],
            }

        # ------------------------------------------------------------------
        # 13. 🟨 KART ÜST (doğrulanmış market: MTID 301)
        # ------------------------------------------------------------------
        # Kart için takım/hakem verimiz yok; bu yüzden uydurma olasılık yerine
        # bahis şirketinin marjdan arındırılmış kendi olasılığını kullanıyoruz.
        for line in ("35", "45"):
            k_ust, k_alt = odds.get(f"kart_{line}_ust"), odds.get(f"kart_{line}_alt")
            if not (k_ust and k_alt):
                continue
            p_k, _ = strip_vig([k_ust, k_alt])
            if p_k[0] >= 0.55 and k_ust >= 1.45:
                signals["kart_korner"] = {
                    "pick": f"Toplam Kart {line[0]}.{line[1]} Üst",
                    "odd": k_ust,
                    "prob": round(p_k[0] * 100, 1),
                    "confidence": round(min(85.0, p_k[0] * 100 * 1.05), 1),
                    "ev_pct": 0.0,
                    "reasons": [
                        f"Piyasa (marjsız) olasılığı %{p_k[0]*100:.1f}. Not: Kart için hakem/takım verisi olmadığından bağımsız model edge'i yoktur.",
                    ],
                }
                break
        # ------------------------------------------------------------------
        # 14. 🔄 İY/MS GERİ DÖNÜŞLER (1/2 - 2/1) & İKİNCİ YARIDA ÇÖZENLER (X/1 - X/2)
        # ------------------------------------------------------------------
        # Efsane Geri Dönüş: Maç tempolu (lambda >= 2.80), KG Var olasılığı yüksek
        if lam_total >= 2.80 and p_btts >= 0.54:
            if odds.get("iyms_1_2") and odds["iyms_1_2"] >= 18.0:
                p_1_2 = max(0.035, min(0.09, (p_1h_btts * 0.45) * (p_away * 0.35)))
                signals["iyms_surpriz"] = {
                    "pick": f"İY/MS 1/2 (İlk Yarı {match['home']} / Maç Sonu {match['away']})",
                    "odd": odds["iyms_1_2"],
                    "prob": round(p_1_2 * 100, 1),
                    "confidence": round(min(89.0, 58.0 + p_1_2 * 350 + chaos_index * 0.2), 1),
                    "ev_pct": round(((p_1_2 * odds["iyms_1_2"]) - 1.0) * 100, 1),
                    "reasons": [
                        f"Efsane Geri Dönüş Radarı: {match['home']} maça hızlı başlayıp öne geçebilir ama maç sonu {match['away']} lehine yüksek hücum baskısı var",
                        f"Kaos Endeksi: {chaos_index:.0f}/100 | İY/MS 1/2 Oranı: {odds['iyms_1_2']:.2f}",
                    ],
                }
            elif odds.get("iyms_2_1") and odds["iyms_2_1"] >= 18.0:
                p_2_1 = max(0.035, min(0.09, (p_1h_btts * 0.45) * (p_home * 0.35)))
                signals["iyms_surpriz"] = {
                    "pick": f"İY/MS 2/1 (İlk Yarı {match['away']} / Maç Sonu {match['home']})",
                    "odd": odds["iyms_2_1"],
                    "prob": round(p_2_1 * 100, 1),
                    "confidence": round(min(89.0, 58.0 + p_2_1 * 350 + chaos_index * 0.2), 1),
                    "ev_pct": round(((p_2_1 * odds["iyms_2_1"]) - 1.0) * 100, 1),
                    "reasons": [
                        f"Efsane Geri Dönüş Radarı: {match['away']} kontradan ilk yarıyı alabilir ancak ev sahibi {match['home']} ikinci yarıda geri dönme gücüne sahip",
                        f"Kaos Endeksi: {chaos_index:.0f}/100 | İY/MS 2/1 Oranı: {odds['iyms_2_1']:.2f}",
                    ],
                }
        elif p_home >= 0.52 and odds.get("iyms_x_1") and odds["iyms_x_1"] >= 3.80:
            # İkinci Yarıda Çözen Favori (X/1)
            p_x_1 = max(0.18, min(0.32, p_draw * 0.55 * p_home * 1.4))
            signals["iyms_surpriz"] = {
                "pick": f"İY/MS X/1 (İlk Yarı Berabere / Maç Sonu {match['home']})",
                "odd": odds["iyms_x_1"],
                "prob": round(p_x_1 * 100, 1),
                "confidence": round(min(93.0, p_x_1 * 100 * 1.8 + p_home * 30), 1),
                "ev_pct": round(((p_x_1 * odds["iyms_x_1"]) - 1.0) * 100, 1),
                "reasons": [
                    f"Favori İkinci Yarıda Çözer: {match['home']} temkinli başlayıp kilidi 2. yarıda açacak profilde",
                    f"Dixon-Coles X/1 olasılığı %{p_x_1*100:.1f} (Oran: {odds['iyms_x_1']:.2f})",
                ],
            }
        elif p_away >= 0.52 and odds.get("iyms_x_2") and odds["iyms_x_2"] >= 3.80:
            # İkinci Yarıda Çözen Deplasman (X/2)
            p_x_2 = max(0.18, min(0.32, p_draw * 0.55 * p_away * 1.4))
            signals["iyms_surpriz"] = {
                "pick": f"İY/MS X/2 (İlk Yarı Berabere / Maç Sonu {match['away']})",
                "odd": odds["iyms_x_2"],
                "prob": round(p_x_2 * 100, 1),
                "confidence": round(min(93.0, p_x_2 * 100 * 1.8 + p_away * 30), 1),
                "ev_pct": round(((p_x_2 * odds["iyms_x_2"]) - 1.0) * 100, 1),
                "reasons": [
                    f"Favori Deplasman İkinci Yarıda Çözer: İlk yarı direnç bekleniyor, 2. yarıda {match['away']} maçı koparır",
                    f"Dixon-Coles X/2 olasılığı %{p_x_2*100:.1f} (Oran: {odds['iyms_x_2']:.2f})",
                ],
            }

        # ------------------------------------------------------------------
        # 15. 🎯 DİXON-COLES KESİN SKOR (CORRECT SCORE) SNIPER
        # ------------------------------------------------------------------
        # Sadece bültende (MTID 777) fiilen AÇIK olan kesin skorları öner!
        matched_score = None
        search_scores = exact_scores if exact_scores else top_3_scores
        for sc, sc_prob in search_scores[:12]:
            score_key = f"skor_{sc.replace('-', '_')}"
            sc_odd = odds.get(score_key)
            if sc_odd and sc_odd > 1.0 and sc_prob >= 0.05:
                matched_score = (sc, sc_prob, sc_odd)
                break

        if matched_score:
            best_score, best_score_prob, score_odd = matched_score
            ev = (best_score_prob * score_odd) - 1.0
            signals["kesin_skor"] = {
                "pick": f"Skor: {best_score} (Dixon-Coles Model Skoru)",
                "odd": score_odd,
                "prob": round(best_score_prob * 100, 1),
                "confidence": round(min(94.0, 65.0 + best_score_prob * 220 + max(0.0, ev * 15)), 1),
                "ev_pct": round(ev * 100, 1),
                "reasons": [
                    f"Dixon-Coles 81 olası skor arasından bültende açık en yüksek yoğunluk: {best_score} (%{best_score_prob*100:.1f})",
                    f"Bülten Kesin Skor Oranı: {score_odd:.2f} (Beklenen Değer: %{ev*100:+.1f})",
                ],
            }

        # ------------------------------------------------------------------
        # 16. ⚖️ TOPLAM GOL ARALIĞI (2-3 GOL & 4-5 GOL)
        # ------------------------------------------------------------------
        p_tg_2_3 = poisson_pmf(2, lam_total) + poisson_pmf(3, lam_total)
        p_tg_4_5 = poisson_pmf(4, lam_total) + poisson_pmf(5, lam_total)
        if odds.get("tg_2_3") and p_tg_2_3 >= 0.44:
            ev = (p_tg_2_3 * odds["tg_2_3"]) - 1.0
            signals["tg_aralik"] = {
                "pick": "Toplam Gol 2-3 Gol (İdeal Bant)",
                "odd": odds["tg_2_3"],
                "prob": round(p_tg_2_3 * 100, 1),
                "confidence": round(min(95.0, p_tg_2_3 * 100 * 1.35 + max(0.0, ev * 30)), 1),
                "ev_pct": round(ev * 100, 1),
                "reasons": [
                    f"İdeal Gol Yoğunluğu: λ={lam_total:.2f} ile maçın tam 2 veya 3 golle bitme ihtimali %{p_tg_2_3*100:.1f}",
                    f"2-3 Gol Oranı: {odds['tg_2_3']:.2f}",
                ],
            }
        elif odds.get("tg_4_5") and p_tg_4_5 >= 0.26 and lam_total >= 3.05:
            ev = (p_tg_4_5 * odds["tg_4_5"]) - 1.0
            signals["tg_aralik"] = {
                "pick": "Toplam Gol 4-5 Gol (Yüksek Bant)",
                "odd": odds["tg_4_5"],
                "prob": round(p_tg_4_5 * 100, 1),
                "confidence": round(min(93.0, p_tg_4_5 * 100 * 1.8 + max(0.0, ev * 30)), 1),
                "ev_pct": round(ev * 100, 1),
                "reasons": [
                    f"Yüksek Skor Bandı: λ={lam_total:.2f} ile maçın 4-5 gol aralığına oturma ihtimali %{p_tg_4_5*100:.1f}",
                    f"4-5 Gol Oranı: {odds['tg_4_5']:.2f}",
                ],
            }

        return signals

    def scan_bulletin(
        self,
        matches: List[Dict[str, Any]],
        strategy_key: str,
        top_n: int = 8,
        fetcher: Optional[Any] = None,
    ) -> List[Dict[str, Any]]:
        """
        Tüm bülteni tarayarak seçilen taktiğe en uygun maçları güven skoruna göre sıralar.
        """
        results: List[Dict[str, Any]] = []
        for m in matches:
            analyzed = self.analyze_match(m, espn_details=None)
            sig = analyzed["signals"].get(strategy_key)
            if sig:
                results.append({
                    "analysis": analyzed,
                    "signal": sig,
                })

        results.sort(key=lambda x: (x["signal"]["confidence"], x["signal"]["prob"]), reverse=True)
        top_results = results[:top_n]

        # Sadece en iyi N adayı ESPN Son 5 Maç / H2H verisiyle zenginleştir (Hız optimizasyonu)
        if fetcher:
            for item in top_results:
                m = item["analysis"]["match"]
                if m.get("espn") and m["espn"].get("event_id"):
                    espn_det = fetcher.fetch_espn_match_details(m["espn"]["event_id"])
                    re_analyzed = self.analyze_match(m, espn_details=espn_det)
                    if strategy_key in re_analyzed["signals"]:
                        item["analysis"] = re_analyzed
                        item["signal"] = re_analyzed["signals"][strategy_key]

        return top_results

    def build_smart_coupons(self, matches: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        """
        Günlük bültenden 5 Kademeli Kombine Kupon (Garantiden Sürprize) +
        2 Adet Ekstra Absürt Oran Sistem Kuponu üretir.
        """
        all_analyzed = [self.analyze_match(m) for m in matches]

        def _get_best_signal(analyzed_item: Dict[str, Any], allowed_keys: List[str]) -> Optional[Dict[str, Any]]:
            best = None
            for k in allowed_keys:
                s = analyzed_item["signals"].get(k)
                if s and (best is None or s["confidence"] > best["confidence"]):
                    best = s
            return best

        used_codes: Set[str] = set()

        def _select_pool(allowed_keys: List[str], min_odd: float, max_odd: float, min_conf: float, limit: int) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
            pool = []
            for a in all_analyzed:
                code = a["match"]["code"]
                if code in used_codes:
                    continue
                s = _get_best_signal(a, allowed_keys)
                if s and min_odd <= s["odd"] <= max_odd and s["confidence"] >= min_conf:
                    pool.append((a, s))
            pool.sort(key=lambda x: (x[1]["confidence"], x[1]["prob"]), reverse=True)
            chosen = pool[:limit]
            for a, _ in chosen:
                used_codes.add(a["match"]["code"])
            return chosen

        # 1. 🟢 Kasa Katlayıcı Garanti Kupon (Düşük Risk, 3 Maç, Oran ~2.00 - 3.20)
        garanti_picks = _select_pool(
            allowed_keys=["ms_banko", "ust_25", "alt_taktigi", "kg_var"],
            min_odd=1.28,
            max_odd=1.65,
            min_conf=65.0,
            limit=3,
        )

        # 2. 🔵 İdeal Dengeli Karma Kupon (Orta-Düşük Risk, 4 Maç, Oran ~4.50 - 7.50)
        ideal_picks = _select_pool(
            allowed_keys=["kg_var", "ust_25", "tg_aralik", "alt_taktigi", "korner", "takim_iki_yari_gol"],
            min_odd=1.45,
            max_odd=1.95,
            min_conf=62.0,
            limit=4,
        )

        # 3. 🟡 Değer / Value Taktik Kuponu (Orta Risk, 4 Maç, Oran ~9.00 - 18.00)
        value_picks = _select_pool(
            allowed_keys=["value_bet", "kombo", "takim_iki_yari_gol", "iy_ust", "ust_25", "kg_var"],
            min_odd=1.60,
            max_odd=2.45,
            min_conf=58.0,
            limit=4,
        )

        # 4. 🟠 Yüksek Oranlı Sert Karma Kupon (Yüksek Risk, 4-5 Maç, Oran ~25.00 - 60.00)
        agresif_picks = _select_pool(
            allowed_keys=["iy_ust", "ust_35", "tg_aralik", "iyms_surpriz", "kombo", "kart_korner"],
            min_odd=1.90,
            max_odd=3.80,
            min_conf=54.0,
            limit=4,
        )

        # 5. 🔴 Bomba Sürpriz Vurgun Kuponu (Çok Yüksek Risk, 4 Maç, Oran ~70.00 - 250.00+)
        bomba_picks = _select_pool(
            allowed_keys=["arti_6_gol", "iki_yari_kg", "iyms_surpriz", "kesin_skor", "ust_35"],
            min_odd=2.40,
            max_odd=12.00,
            min_conf=50.0,
            limit=4,
        )

        # 6. 🎰 SİSTEM PAKETİ 1 (8 Maçta Sistem 4-5-6 "Kaos & Geri Dönüş")
        # İY/MS 2/1 & 1/2 + +6 Gol + İki Yarıda KG Var
        sistem_8_pool = []
        for a in all_analyzed:
            for k in ["iyms_surpriz", "arti_6_gol", "iki_yari_kg"]:
                s = a["signals"].get(k)
                if s and s["odd"] >= 2.50:
                    sistem_8_pool.append((a, s))
                    break
        sistem_8_pool.sort(key=lambda x: (x[1]["confidence"], x[1]["odd"]), reverse=True)
        sistem_8_picks = sistem_8_pool[:8]

        # 7. 🌌 SİSTEM PAKETİ 2 (10 Maçta Sistem 4-5 "Kesin Skor & Ultra Bomba")
        # Kesin Skorlar + İY/MS 1/2-2/1 + +6 Gol
        sistem_10_pool = []
        for a in all_analyzed:
            for k in ["kesin_skor", "iyms_surpriz", "arti_6_gol", "iki_yari_kg"]:
                s = a["signals"].get(k)
                if s and s["odd"] >= 3.20:
                    sistem_10_pool.append((a, s))
                    break
        sistem_10_pool.sort(key=lambda x: (x[1]["odd"] * 0.4 + x[1]["confidence"] * 0.6), reverse=True)
        sistem_10_picks = sistem_10_pool[:10]

        def _pack_coupon(title: str, desc: str, items: List[Tuple[Dict[str, Any], Dict[str, Any]]]) -> Dict[str, Any]:
            total_odd = 1.0
            picks = []
            for a, s in items:
                total_odd *= s["odd"]
                picks.append({
                    "match": f"{a['match']['home']} - {a['match']['away']}",
                    "league": a["match"]["league"],
                    "time": f"{a['match']['date']} {a['match']['time']}",
                    "pick": s["pick"],
                    "odd": s["odd"],
                    "prob": s["prob"],
                    "reason": s["reasons"][0] if s["reasons"] else "",
                })
            return {
                "title": title,
                "desc": desc,
                "total_odd": round(total_odd, 2),
                "picks": picks,
            }

        def _calc_system_combinations(n: int, k: int) -> int:
            return math.comb(n, k)

        # Sistem 8 İstatistikleri
        c_8_4 = _calc_system_combinations(len(sistem_8_picks), 4) if len(sistem_8_picks) >= 4 else 0
        c_8_5 = _calc_system_combinations(len(sistem_8_picks), 5) if len(sistem_8_picks) >= 5 else 0
        avg_odd_s8 = sum(s["odd"] for _, s in sistem_8_picks) / max(1, len(sistem_8_picks))
        sample_win_s8_4 = round((avg_odd_s8 ** 4), 1)

        # Sistem 10 İstatistikleri
        c_10_4 = _calc_system_combinations(len(sistem_10_picks), 4) if len(sistem_10_picks) >= 4 else 0
        c_10_5 = _calc_system_combinations(len(sistem_10_picks), 5) if len(sistem_10_picks) >= 5 else 0
        avg_odd_s10 = sum(s["odd"] for _, s in sistem_10_picks) / max(1, len(sistem_10_picks))
        sample_win_s10_4 = round((avg_odd_s10 ** 4), 1)

        return {
            "kupon_1_garanti": _pack_coupon(
                "🟢 1. KASA KATLAYICI GARANTİ KUPON (Düşük Risk)",
                "Matematiksel tutma olasılığı en yüksek (%65+) banko tercihlerden oluşur.",
                garanti_picks,
            ),
            "kupon_2_ideal": _pack_coupon(
                "🔵 2. İDEAL DENGELİ KARMA KUPON (Orta-Düşük Risk)",
                "2.5 Alt, KG Var, 2-3 Gol, Ev 1.5 Üst ve Korner tercihlerinden oluşan ideal denge kuponu.",
                ideal_picks,
            ),
            "kupon_3_value": _pack_coupon(
                "🟡 3. DEĞER & TAKTİK KOMBİNE KUPONU (Orta Risk)",
                "Sharp Value (+EV), Kombo (MS&Üst/KG&Üst) ve İki Yarıda Gol marketlerinden oluşan değer kuponu.",
                value_picks,
            ),
            "kupon_4_agresif": _pack_coupon(
                "🟠 4. YÜKSEK ORANLI SERT KARMA KUPON (Yüksek Risk)",
                "İY/MS X/1-X/2, 3.5 Üst, İY 1.5 Üst ve 4-5 Gol tercihlerinden oluşan yüksek çarpan kuponu.",
                agresif_picks,
            ),
            "kupon_5_bomba": _pack_coupon(
                "🔴 5. BOMBA SÜRPRİZ VURGUN KUPONU (Çok Yüksek Risk)",
                "+6 Gol, İki Yarıda da KG Var (Evet/Evet) ve Kesin Skorlardan oluşan devasa oran kuponu.",
                bomba_picks,
            ),
            "sistem_8_kaos": {
                "title": "🎰 6. SİSTEM PAKETİ: 8 MAÇTA SİSTEM 4-5-6 (Kaos & Geri Dönüş)",
                "desc": "İY/MS 2/1 & 1/2 + +6 Gol + İki Yarıda KG Var karması 8 yüksek oranlı maç.",
                "system_formula": f"Önerilen Sistem: 4 ve 5 (Sistem 4 = {c_8_4} Kolon, Sistem 5 = {c_8_5} Kolon | Toplam: {c_8_4 + c_8_5} Kolon)",
                "sample_payout": f"4 Maç Tutarsa Ortalama Çarpan: ~{sample_win_s8_4:.1f}x (5 maçta devasa katlama)",
                "picks": _pack_coupon("", "", sistem_8_picks)["picks"],
            },
            "sistem_10_skor": {
                "title": "🌌 7. SİSTEM PAKETİ: 10 MAÇTA SİSTEM 4-5 (Kesin Skor & Ultra Bomba)",
                "desc": "Dixon-Coles Yüksek Skorlar + Efsane Geri Dönüşler (1/2 - 2/1) karması 10 maç.",
                "system_formula": f"Önerilen Sistem: 4 veya 5 (Sistem 4 = {c_10_4} Kolon, Sistem 5 = {c_10_5} Kolon)",
                "sample_payout": f"4 Maç Tutarsa Ortalama Çarpan: ~{sample_win_s10_4:.1f}x | 5 Maç Tutarsa Rekor Çarpan",
                "picks": _pack_coupon("", "", sistem_10_picks)["picks"],
            },
        }

    @staticmethod
    def get_bankroll_plan(total_bankroll: float = 1000.0) -> List[Dict[str, Any]]:
        """1.000 ₺ (veya kullanıcının girdiği bakiye) için optimal portföy dağıtım kılavuzu."""
        ratios = [
            ("🟢 1. Kasa Katlayıcı Garanti", 0.35, 2.30),
            ("🔵 2. İdeal Dengeli Karma", 0.25, 5.50),
            ("🟡 3. Değer & Value Taktik", 0.15, 12.0),
            ("🟠 4. Yüksek Oranlı Sert Karma", 0.10, 35.0),
            ("🔴 5. Bomba Sürpriz Vurgun", 0.05, 120.0),
            ("🎰 6. Sistem 8 (Kaos & Geri Dönüş)", 0.05, 450.0),
            ("🌌 7. Sistem 10 (Kesin Skor & Ultra)", 0.05, 850.0),
        ]
        plan = []
        for name, pct, avg_odd in ratios:
            stake = round(total_bankroll * pct, 1)
            target_win = round(stake * avg_odd, 1)
            plan.append({
                "name": name,
                "pct": int(pct * 100),
                "stake": stake,
                "target_win": target_win,
            })
        return plan
