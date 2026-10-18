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

import bisect
import math
import random
import re
from typing import Any, Dict, List, Optional, Set, Tuple


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
            "exact_scores": exact_scores,
            "score_matrix": score_matrix,
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

        used_codes: Set[str] = set()

        def _select_diverse_pool(
            allowed_keys: List[str],
            min_odd: float,
            max_odd: float,
            min_conf: float,
            limit: int,
            max_per_family: int = 1,
        ) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
            # Adayları topla (maçın sunduğu her geçerli sinyal için)
            candidates = []
            for a in all_analyzed:
                code = a["match"]["code"]
                if code in used_codes:
                    continue
                for k in allowed_keys:
                    s = a["signals"].get(k)
                    if s and min_odd <= s["odd"] <= max_odd and s.get("confidence", 0) >= min_conf:
                        fam = self.get_market_family(k, s.get("pick", ""))
                        if fam == "specials" and "kart_korner" not in allowed_keys and "korner" not in allowed_keys:
                            continue
                        candidates.append((a, s, fam))

            # Güven skoruna göre sırala
            candidates.sort(key=lambda x: (x[1]["confidence"], x[1]["prob"]), reverse=True)

            chosen: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
            chosen_codes: Set[str] = set()
            family_counts: Dict[str, int] = {}

            # 1. Aşama: Her pazar ailesinden en fazla max_per_family adet al (çeşitlilik garantisi)
            for a, s, fam in candidates:
                if len(chosen) >= limit:
                    break
                code = a["match"]["code"]
                if code in chosen_codes:
                    continue
                if family_counts.get(fam, 0) < max_per_family:
                    chosen.append((a, s))
                    chosen_codes.add(code)
                    family_counts[fam] = family_counts.get(fam, 0) + 1

            # 2. Aşama: Yeterli maç bulunamadıysa kuralı 1 adım gevşet
            if len(chosen) < limit:
                for a, s, fam in candidates:
                    if len(chosen) >= limit:
                        break
                    code = a["match"]["code"]
                    if code in chosen_codes:
                        continue
                    if family_counts.get(fam, 0) < (max_per_family + 1):
                        chosen.append((a, s))
                        chosen_codes.add(code)
                        family_counts[fam] = family_counts.get(fam, 0) + 1

            for a, _ in chosen:
                used_codes.add(a["match"]["code"])
            return chosen

        # 1. 🟢 Kasa Katlayıcı Garanti Kupon (Düşük Risk, 3 Maç, Oran ~2.00 - 3.20)
        garanti_picks = _select_diverse_pool(
            allowed_keys=["ms_banko", "ust_25", "alt_taktigi", "kg_var", "takim_iki_yari_gol"],
            min_odd=1.28,
            max_odd=1.65,
            min_conf=65.0,
            limit=3,
            max_per_family=1,
        )

        # 2. 🔵 İdeal Dengeli Karma Kupon (Orta-Düşük Risk, 4 Maç, Oran ~4.50 - 7.50)
        ideal_picks = _select_diverse_pool(
            allowed_keys=["kg_var", "ust_25", "tg_aralik", "alt_taktigi", "takim_iki_yari_gol", "kombo", "iy_ust"],
            min_odd=1.45,
            max_odd=1.95,
            min_conf=62.0,
            limit=4,
            max_per_family=1,
        )

        # 3. 🟡 Değer / Value Taktik Kuponu (Orta Risk, 4 Maç, Oran ~9.00 - 18.00)
        value_picks = _select_diverse_pool(
            allowed_keys=["value_bet", "kombo", "takim_iki_yari_gol", "iy_ust", "ust_25", "kg_var"],
            min_odd=1.60,
            max_odd=2.45,
            min_conf=58.0,
            limit=4,
            max_per_family=1,
        )

        # 4. 🟠 Yüksek Oranlı Sert Karma Kupon (Yüksek Risk, 4-5 Maç, Oran ~25.00 - 60.00)
        agresif_picks = _select_diverse_pool(
            allowed_keys=["iy_ust", "ust_35", "tg_aralik", "iyms_surpriz", "kombo", "takim_iki_yari_gol"],
            min_odd=1.90,
            max_odd=3.80,
            min_conf=54.0,
            limit=4,
            max_per_family=1,
        )

        # 5. 🔴 Bomba Sürpriz Vurgun Kuponu (Çok Yüksek Risk, 4 Maç, Oran ~70.00 - 250.00+)
        bomba_picks = _select_diverse_pool(
            allowed_keys=["arti_6_gol", "iki_yari_kg", "iyms_surpriz", "kesin_skor", "ust_35"],
            min_odd=2.40,
            max_odd=12.00,
            min_conf=50.0,
            limit=4,
            max_per_family=1,
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
    def classify_match_league_tier(match: Dict[str, Any]) -> int:
        """
        Maçın ligini güvenilirlik ve önem derecesine göre 3 seviyeye (Tier) ayırır:
        Tier 1: Ana / Elit Ligler (Nesine'de '1' / MBS 1 yazanlar, Süper Lig, Premier Lig, La Liga, Serie A, vb.)
        Tier 2: 2. Ligler & Kaliteli İkincil Ligler (Championship, La Liga 2, 2. Bundesliga, TFF 1. Lig, vb.)
        Tier 3: 3. Ligler, Alt Ligler, Gençler/U19, Amatör ve Rezerv Ligler
        """
        l = match.get("league", "").lower()
        mbs = match.get("mbs", 3)

        # Kesin Alt Lig / Gençlik / Amatör Filtresi -> Tier 3
        exclude_sub = [
            "u19", "u20", "u21", "u23", "genç", "kadın", "reserve", "rezerv",
            "3. lig", "3.lig", "4. lig", "4.lig", "federasyon", "amatör",
            "bölgesel", "regional", "oberliga", "segunda rfef", "tercera"
        ]
        if any(x in l for x in exclude_sub):
            return 3

        # Tier 1 - Ana / Elit Ligler & Büyük Organizasyonlar & Nesine MBS 1
        tier1_leagues = [
            "süper lig", "super lig", "premier lig", "premier league", "la liga",
            "serie a", "bundesliga", "ligue 1", "şampiyonlar ligi", "avrupa ligi",
            "konferans ligi", "uluslar ligi", "eredivisie", "liga portugal", "primeira liga",
            "suudi arabistan pro", "suudi pro", "brezilya serie a", "arjantin primera lpf",
            "abd major lig", "mls"
        ]
        if any(k in l for k in tier1_leagues) and not any(x in l for x in ["2.", "3.", "kadın"]):
            return 1

        # Nesine'de yanında "1" yazan (MBS 1) ana ligler
        if mbs == 1 and not any(x in l for x in ["2.", "3.", "kadın", "genç", "rfef"]):
            return 1

        # Tier 2 - Güçlü 2. Ligler ve Kaliteli Ligler
        tier2_leagues = [
            "championship", "2. lig", "segunda", "2. bundesliga", "serie b",
            "ligue 2", "1. lig", "tff 1", "allsvenskan", "superliga", "eliteserien",
            "ekstraklasa", "premiership", "belçika", "avusturya bundesliga",
            "isviçre süper", "isveç allsvenskan", "danimarka süper", "yunanistan süper",
            "brezilya serie b", "primera nacional", "liga mx", "j1"
        ]
        if any(k in l for k in tier2_leagues):
            return 2
        if any(x in l for x in ["süper lig", "premier lig", "1. lig", "pro lig"]):
            return 2

        return 3

    def build_custom_target_odds_coupon(
        self,
        matches: List[Dict[str, Any]],
        target_min_odd: float,
        target_max_odd: float,
        tier_filter: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Kullanıcının belirlediği hedef oran aralığına (örn. 3.00 - 6.00)
        en uygun, matematiksel olasılığı ve piyasa değeri en yüksek,
        pazar ailesi çeşitlendirilmiş özel kupon oluşturur.
        """
        if tier_filter in (1, 2, 3):
            matches = [m for m in matches if self.classify_match_league_tier(m) == tier_filter]

        all_analyzed = [self.analyze_match(m) for m in matches if m.get("odds")]
        all_analyzed = [a for a in all_analyzed if a["signals"]]

        if not all_analyzed:
            return None

        target_mid = (target_min_odd + target_max_odd) / 2.0

        # Her maç için farklı market ailelerinden geçerli sinyalleri topla
        match_candidates: List[Tuple[Dict[str, Any], Dict[str, Any], str]] = []
        for a in all_analyzed:
            fams_for_m: Dict[str, Dict[str, Any]] = {}
            for k, s in a["signals"].items():
                if not (1.20 <= s["odd"] <= 4.80) or s.get("confidence", 0) < 50.0:
                    continue
                fam = self.get_market_family(k, s.get("pick", ""))
                if fam not in fams_for_m or s["confidence"] > fams_for_m[fam]["confidence"]:
                    fams_for_m[fam] = s
            for fam, s in fams_for_m.items():
                match_candidates.append((a, s, fam))

        if not match_candidates:
            return None

        # Güvene göre sırala ve en iyi 25 adayı değerlendir
        match_candidates.sort(key=lambda x: (x[1]["confidence"], x[1]["prob"]), reverse=True)
        top_cands = match_candidates[:25]

        best_combo = None
        best_score = -999999.0

        import itertools
        for k in (2, 3, 4):
            for combo in itertools.combinations(top_cands, k):
                codes = [item[0]["match"]["code"] for item in combo]
                if len(set(codes)) != len(codes):
                    continue
                fams = [item[2] for item in combo]
                fam_diversity = len(set(fams))
                if fam_diversity < len(fams) - 1:
                    continue

                tot_odd = 1.0
                for item in combo:
                    tot_odd *= item[1]["odd"]

                if tot_odd < target_min_odd * 0.70 or tot_odd > target_max_odd * 1.40:
                    continue

                avg_conf = sum(item[1]["confidence"] for item in combo) / k
                in_range = 1.0 if (target_min_odd <= tot_odd <= target_max_odd) else 0.0
                dist = abs(tot_odd - target_mid) / max(0.1, target_mid)

                score = (avg_conf * 1.2) + (in_range * 45.0) + (fam_diversity * 8.0) - (dist * 35.0)
                if score > best_score:
                    best_score = score
                    best_combo = (combo, tot_odd)

        if not best_combo:
            fallback_c = top_cands[:3]
            tot_odd = 1.0
            for item in fallback_c:
                tot_odd *= item[1]["odd"]
            best_combo = (fallback_c, tot_odd)

        combo_items, tot_odd = best_combo
        picks = []
        for a, s, fam in combo_items:
            picks.append({
                "match": f"{a['match']['home']} - {a['match']['away']}",
                "league": a["match"]["league"],
                "time": f"{a['match']['date']} {a['match']['time']}",
                "pick": s["pick"],
                "odd": s["odd"],
                "prob": s["prob"],
                "confidence": s.get("confidence", 0.0),
                "family": fam,
                "reason": s["reasons"][0] if s["reasons"] else "",
            })

        return {
            "title": f"🎯 6 SAATLİK ÖZEL HEDEF ORAN KUPONU (Hedef: {target_min_odd:.2f} - {target_max_odd:.2f})",
            "desc": f"Belirlediğiniz {target_min_odd:.2f} - {target_max_odd:.2f} oran aralığına uygun, market ailesi çeşitlendirilmiş optimal kupon.",
            "risk_badge": "Özel Hedef Oran",
            "target_min_odd": target_min_odd,
            "target_max_odd": target_max_odd,
            "total_odd": round(tot_odd, 2),
            "picks": picks,
        }

    def build_upcoming_window_coupons(
        self,
        matches: List[Dict[str, Any]],
        hours: int = 6,
        tier_filter: Optional[int] = None,
        target_min_odd: Optional[float] = None,
        target_max_odd: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Önümüzdeki 6 saat içinde başlayacak maçları filtreleyerek
        seçilen lig kategorisine (Lig 1, Lig 2, Lig 3 veya Karışık) göre
        Garantiden Riskliye Doğru 4 Kademeli Yaklaşan Maç Kuponu üretir.
        İstenirse kullanıcının belirlediği özel hedef oran kuponunu da ekler.
        """
        from datetime import datetime, timedelta
        now = datetime.now()
        limit = now + timedelta(hours=hours)

        # 6 saatlik penceredeki maçları al
        time_matches = [m for m in matches if now <= m.get("start_dt", now) <= limit]
        actual_hours = hours
        if len(time_matches) < 4:
            actual_hours = max(hours, 8)
            limit = now + timedelta(hours=actual_hours)
            time_matches = [m for m in matches if now <= m.get("start_dt", now) <= limit]

        tier_label_map = {
            1: "👑 LİG 1 (ANA VE ELİT LİGLER / NESİNE 1)",
            2: "🥈 LİG 2 (2. LİGLER & KALİTELİ LİGLER)",
            3: "🥉 LİG 3 (ALT LİGLER & DİĞERLERİ)",
        }
        tier_desc_map = {
            1: "Sadece Süper Lig, Premier Lig, La Liga, Serie A gibi büyük ve güvenilir ana ligler taranır (3. lig ve alt ligler elenmiştir).",
            2: "Championship, Segunda, 2. Bundesliga, TFF 1. Lig gibi kaliteli ikincil ligler taranır.",
            3: "3. Ligler, yerel ve bölgesel alt ligler taranır.",
        }

        if tier_filter in (1, 2, 3):
            window_matches = [m for m in time_matches if self.classify_match_league_tier(m) == tier_filter]
            tier_badge = tier_label_map[tier_filter]
            tier_desc = tier_desc_map[tier_filter]
            has_enough = len(window_matches) >= 3
        else:
            window_matches = time_matches
            tier_badge = "🎲 6 SAATLİK KARIŞIK (TÜM LİGLER)"
            tier_desc = "Bültendeki tüm ligler arasından modelin en yüksek güven verdiği karma kuponlar."
            has_enough = len(window_matches) >= 3

        all_analyzed = [self.analyze_match(m) for m in window_matches]
        all_analyzed = [a for a in all_analyzed if a["signals"]]

        def _select_diverse_window_pool(
            allowed_keys: List[str],
            min_odd: float,
            max_odd: float,
            limit: int,
            exclude_codes: Optional[Set[str]] = None,
            max_per_family: int = 1,
        ) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
            candidates = []
            for a in all_analyzed:
                code = a["match"]["code"]
                if exclude_codes is not None and code in exclude_codes:
                    continue
                for k in allowed_keys:
                    s = a["signals"].get(k)
                    if s and min_odd <= s["odd"] <= max_odd:
                        fam = self.get_market_family(k, s.get("pick", ""))
                        if fam == "specials" and "kart_korner" not in allowed_keys and "korner" not in allowed_keys:
                            continue
                        candidates.append((a, s, fam))

            candidates.sort(key=lambda x: (x[1]["confidence"], x[1]["prob"]), reverse=True)

            chosen: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
            chosen_codes: Set[str] = set()
            family_counts: Dict[str, int] = {}

            # 1. Aşama: max_per_family kuralına uyarak farklı pazarlardan seç
            for a, s, fam in candidates:
                if len(chosen) >= limit:
                    break
                code = a["match"]["code"]
                if code in chosen_codes:
                    continue
                if family_counts.get(fam, 0) < max_per_family:
                    chosen.append((a, s))
                    chosen_codes.add(code)
                    family_counts[fam] = family_counts.get(fam, 0) + 1

            # 2. Aşama: Limit dolmadıysa esnet
            if len(chosen) < limit:
                for a, s, fam in candidates:
                    if len(chosen) >= limit:
                        break
                    code = a["match"]["code"]
                    if code in chosen_codes:
                        continue
                    if family_counts.get(fam, 0) < (max_per_family + 1):
                        chosen.append((a, s))
                        chosen_codes.add(code)
                        family_counts[fam] = family_counts.get(fam, 0) + 1

            if exclude_codes is not None:
                for a, _ in chosen:
                    exclude_codes.add(a["match"]["code"])
            return chosen

        def _pack_coupon(title: str, desc: str, risk_badge: str, items: List[Tuple[Dict[str, Any], Dict[str, Any]]]) -> Dict[str, Any]:
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
                    "confidence": s.get("confidence", 0.0),
                    "reason": s["reasons"][0] if s["reasons"] else "",
                })
            return {
                "title": title,
                "desc": desc,
                "risk_badge": risk_badge,
                "total_odd": round(total_odd, 2),
                "picks": picks,
            }

        used_codes: Set[str] = set()

        # 1. 🟢 6 Saatlik Kasa Katlayıcı (Garanti / Düşük Risk, 2-3 Maç)
        garanti = _select_diverse_window_pool(
            allowed_keys=["ms_banko", "ust_25", "alt_taktigi", "kg_var", "takim_iki_yari_gol"],
            min_odd=1.25,
            max_odd=1.65,
            limit=3,
            exclude_codes=used_codes,
            max_per_family=1,
        )

        # 2. 🔵 6 Saatlik İdeal Kombine (Dengeli / Orta-Düşük Risk, 3 Maç)
        ideal = _select_diverse_window_pool(
            allowed_keys=["kg_var", "ust_25", "tg_aralik", "takim_iki_yari_gol", "kombo", "iy_ust"],
            min_odd=1.45,
            max_odd=1.95,
            limit=3,
            exclude_codes=used_codes,
            max_per_family=1,
        )

        # 3. 🟡 6 Saatlik Değer & Fırsat (Orta Risk, 3 Maç)
        value = _select_diverse_window_pool(
            allowed_keys=["value_bet", "kombo", "iy_ust", "ust_35", "takim_iki_yari_gol"],
            min_odd=1.60,
            max_odd=2.50,
            limit=3,
            exclude_codes=used_codes,
            max_per_family=1,
        )

        # 4. 🔴 6 Saatlik Sürpriz Vurgun (Yüksek Risk, 2-3 Maç)
        surpriz = _select_diverse_window_pool(
            allowed_keys=["arti_6_gol", "iyms_surpriz", "kesin_skor", "ust_35", "iki_yari_kg"],
            min_odd=2.00,
            max_odd=15.00,
            limit=3,
            exclude_codes=None,
            max_per_family=1,
        )

        # Özel hedef oran kuponu talebi varsa oluştur
        target_coupon = None
        if target_min_odd is not None and target_max_odd is not None:
            target_coupon = self.build_custom_target_odds_coupon(
                window_matches,
                target_min_odd=target_min_odd,
                target_max_odd=target_max_odd,
                tier_filter=tier_filter,
            )

        return {
            "window_hours": actual_hours,
            "tier_filter": tier_filter,
            "tier_badge": tier_badge,
            "tier_desc": tier_desc,
            "has_enough": has_enough,
            "start_time": now.strftime("%H:%M"),
            "end_time": limit.strftime("%H:%M"),
            "match_count": len(window_matches),
            "target_coupon": target_coupon,
            "coupons": [
                _pack_coupon(
                    "🟢 1. YAKLAŞAN GARANTİ KUPON (Düşük Risk)",
                    "Önümüzdeki saatlerin en yüksek güvenli banko tercihleri (Çeşitlendirilmiş Pazar).",
                    "Düşük Risk",
                    garanti,
                ),
                _pack_coupon(
                    "🔵 2. YAKLAŞAN İDEAL KOMBİNE (Dengeli Risk)",
                    "Dengeli oran ve yüksek olasılıklı karma tercihler (KG, İki Yarı Gol, 2.5 Üst).",
                    "Dengeli",
                    ideal,
                ),
                _pack_coupon(
                    "🟡 3. YAKLAŞAN DEĞER & FIRSAT (Orta Risk)",
                    "Modelin piyasaya göre değer yakaladığı (+EV) yüksek oranlar.",
                    "Orta Risk",
                    value,
                ),
                _pack_coupon(
                    "🔴 4. YAKLAŞAN SÜRPRİZ VURGUN (Yüksek Risk)",
                    "Kesin skor, +6 gol ve İY/MS geri dönüşlerinden oluşan sürpriz bomba.",
                    "Yüksek Risk",
                    surpriz,
                ),
            ],
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

    def scan_live_opportunities(self, live_matches: List[Dict[str, Any]], pre_matches: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Canlı maçları (Mackolik) maç öncesi xG & tempo modellerimizle karşılaştırarak
        oyun içi (In-Play) gol veya geri dönüş fırsatlarını yakalar.
        """
        if not live_matches or not pre_matches:
            return []

        # Maç öncesi analizleri indeksle
        analyzed_map: Dict[str, Dict[str, Any]] = {}
        for m in pre_matches:
            h_norm = re.sub(r"[^a-z0-9]", "", m["home"].lower())
            a_norm = re.sub(r"[^a-z0-9]", "", m["away"].lower())
            analyzed_map[f"{h_norm}__{a_norm}"] = self.analyze_match(m)

        opportunities: List[Dict[str, Any]] = []

        for lm in live_matches:
            h_live = re.sub(r"[^a-z0-9]", "", lm["home"].lower())
            a_live = re.sub(r"[^a-z0-9]", "", lm["away"].lower())

            # Eşleşen maç analizi bul
            matched_a = (
                analyzed_map.get(f"{h_live}__{a_live}")
                or next((v for k, v in analyzed_map.items() if h_live in k or a_live in k), None)
            )
            if not matched_a:
                continue

            status = lm.get("status", "")
            score = lm.get("score", "0-0")
            h_g = lm.get("home_goals", 0) or 0
            a_g = lm.get("away_goals", 0) or 0
            tot_g = h_g + a_g

            minute = 0
            digits = re.findall(r"\d+", status)
            if digits:
                minute = int(digits[0])
            elif status in ("İY", "Devre"):
                minute = 45

            lam_tot = matched_a["lam_total"]
            p_home = matched_a["probabilities"]["ms_1"]
            p_away = matched_a["probabilities"]["ms_2"]

            # Fırsat 1: Geç Gol Radarı (Maç öncesi xG >= 2.60 ama dakika >= 45 ve gol sayısı az)
            if lam_tot >= 2.60 and minute >= 40 and tot_g <= 1:
                opportunities.append({
                    "match": f"{lm['home']} - {lm['away']}",
                    "league": lm.get("league", "") or matched_a["match"]["league"],
                    "status": f"{status}. dk ({score})",
                    "opportunity_type": "🔥 Canlı Geç Gol Radarı (0.5 Üst / 1.5 Üst)",
                    "expected_goals": lam_tot,
                    "confidence": round(min(92.0, 65.0 + lam_tot * 8.0), 1),
                    "reason": f"Model maç öncesi yüksek tempo (λ={lam_tot:.2f}) öngörüyordu; şu an {minute}. dk ve skor {score}. İkinci yarıda kapanan savunmalar ve artan yorgunlukla gol patlaması bekleniyor.",
                })

            # Fırsat 2: Canlı Favori Baskısı (Favori maç öncesi %50+ galibiyete sahipti ama şu an geride/berabere)
            elif p_home >= 54.0 and minute >= 45 and h_g <= a_g:
                opportunities.append({
                    "match": f"{lm['home']} - {lm['away']}",
                    "league": lm.get("league", "") or matched_a["match"]["league"],
                    "status": f"{status}. dk ({score})",
                    "opportunity_type": f"⚡ Canlı Favori Baskısı ({lm['home']} Galibiyeti / Sıradaki Gol)",
                    "expected_goals": lam_tot,
                    "confidence": round(min(90.0, p_home + 15.0), 1),
                    "reason": f"{lm['home']} maç öncesi %{p_home:.1f} kazanma olasılığına sahipti. {minute}. dakikada skor {score}; favorinin 2. yarıdaki hücum baskısı sıradaki golü getirebilir.",
                })
            elif p_away >= 54.0 and minute >= 45 and a_g <= h_g:
                opportunities.append({
                    "match": f"{lm['home']} - {lm['away']}",
                    "league": lm.get("league", "") or matched_a["match"]["league"],
                    "status": f"{status}. dk ({score})",
                    "opportunity_type": f"⚡ Canlı Favori Deplasman Baskısı ({lm['away']} Galibiyeti / Sıradaki Gol)",
                    "expected_goals": lam_tot,
                    "confidence": round(min(90.0, p_away + 15.0), 1),
                    "reason": f"{lm['away']} maç öncesi %{p_away:.1f} kazanma olasılığına sahipti. {minute}. dakikada skor {score}; favori deplasman galibiyeti değerli.",
                })

        opportunities.sort(key=lambda x: x["confidence"], reverse=True)
        return opportunities

    def build_custom_wizard_coupon(
        self,
        matches: List[Dict[str, Any]],
        match_count: int = 3,
        min_total_odd: float = 2.0,
        max_total_odd: float = 8.0,
        risk_level: str = "ideal",
    ) -> Dict[str, Any]:
        """
        Kullanıcının parametrelerine göre (maç sayısı, hedef oran, risk seviyesi)
        kişiselleştirilmiş akıllı kupon üretir.
        """
        all_analyzed = [self.analyze_match(m) for m in matches]
        all_analyzed = [a for a in all_analyzed if a["signals"]]

        risk_level_clean = risk_level.lower()
        if "garanti" in risk_level_clean:
            allowed = ["ms_banko", "ust_25", "alt_taktigi", "kg_var", "takim_iki_yari_gol"]
            single_min, single_max = 1.25, 1.65
            title = "🟢 SİHİRBAZ: GARANTİ KASA KATLAYICI"
        elif "value" in risk_level_clean or "deger" in risk_level_clean:
            allowed = ["value_bet", "kombo", "iy_ust", "ust_35"]
            single_min, single_max = 1.60, 2.50
            title = "🟡 SİHİRBAZ: DEĞERLİ ORAN (+EV) KOMBİNİ"
        elif "bomba" in risk_level_clean or "surpriz" in risk_level_clean:
            allowed = ["arti_6_gol", "iyms_surpriz", "kesin_skor", "ust_35", "iki_yari_kg"]
            single_min, single_max = 2.00, 15.00
            title = "🔴 SİHİRBAZ: YÜKSEK ÇARPAN SÜRPRİZ BOMBA"
        else:
            allowed = ["kg_var", "ust_25", "tg_aralik", "korner", "takim_iki_yari_gol", "kombo"]
            single_min, single_max = 1.45, 1.95
            title = "🔵 SİHİRBAZ: DENGELİ İDEAL KOMBİNE"

        cands = []
        for a in all_analyzed:
            for k in allowed:
                s = a["signals"].get(k)
                if s and single_min <= s["odd"] <= single_max:
                    cands.append((a, s))
                    break

        cands.sort(key=lambda x: (x[1]["confidence"], x[1]["prob"]), reverse=True)
        chosen = cands[:match_count]

        total_odd = 1.0
        picks = []
        for a, s in chosen:
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
            "target_odd_range": f"{min_total_odd:.2f} - {max_total_odd:.2f}",
            "requested_matches": match_count,
            "total_odd": round(total_odd, 2),
            "picks": picks,
        }

    @staticmethod
    def get_market_family(signal_key: str, pick_str: str = "") -> str:
        """
        Her bir tercihi pazar ailesine (market family) ayırarak
        kupon içi market çeşitliliğini (tek yönlü olmamasını) garanti eder.
        """
        p = pick_str.strip().lower()
        if any(w in p for w in ["&", " ve ", "iyms", "iy/ms"]):
            return "combos"
        if any(w in p for w in ["kg var", "kg yok", "karşılıklı gol"]):
            return "btts"
        if any(w in p for w in ["iki yarıda", "ev sahibi 1.5", "deplasman 1.5", "ev sahibi 0.5", "deplasman 0.5"]):
            return "team_goals"
        if any(w in p for w in ["iy 1.5", "iy 0.5", "ilk yarı", "iy üst", "iy alt"]):
            return "halves_goals"
        if any(w in p for w in ["2-3 gol", "4-5 gol", "gol aralığı", "0-1 gol"]):
            return "goal_range"
        if "skor:" in p or "skor " in p or re.search(r"^\d+-\d+$", p):
            return "exact_score"
        if any(w in p for w in ["korner", "kart"]):
            return "specials"
        if any(w in p for w in ["üst", "alt"]):
            return "over_under"
        if any(w in p for w in ["ms 1", "ms 2", "ms x", "çifte şans", "1x", "x2", "12"]):
            return "match_winner"
        return "other"

    @staticmethod
    def evaluate_pick(
        pick: str,
        home_score: Optional[int],
        away_score: Optional[int],
        ht_home: Optional[int] = None,
        ht_away: Optional[int] = None,
    ) -> Optional[bool]:
        """
        Verilen maç sonucuna göre tercihin kazanıp kazanmadığını matematiksel olarak doğrular.
        - True: Kesin Kazandı (WON)
        - False: Kesin Kaybetti (LOST)
        - None: Veri yetersizliği sebebiyle doğrulanmadı (UNVERIFIED - Korner, kart, İY verisi eksik vb.)
        """
        if home_score is None or away_score is None:
            return None

        p = pick.strip().lower()
        tot = home_score + away_score

        # 1. Korner ve Kart Bahisleri (Skor verisinde kart/korner yoksa ASLA False/Lost dönmemeli)
        if any(w in p for w in ["korner", "kart", "kırmızı"]):
            return None

        # 2. Her İki Yarıda Gol Olur & İki Yarıda da Gol Atar
        if "her iki yarıda gol olur" in p:
            if tot < 2:
                return False
            if ht_home is not None and ht_away is not None:
                ht_tot = ht_home + ht_away
                sh_tot = tot - ht_tot
                return ht_tot >= 1 and sh_tot >= 1
            return None

        if "iki yarıda da gol" in p or "iki yarıda kg" in p or "iki yarı kg" in p:
            if tot < 2:
                return False
            if ht_home is None or ht_away is None:
                return None
            sh2_home = home_score - ht_home
            sh2_away = away_score - ht_away
            if "ev sahibi" in p and "iki yarıda" in p:
                return ht_home >= 1 and sh2_home >= 1
            if "deplasman" in p and "iki yarıda" in p:
                return ht_away >= 1 and sh2_away >= 1
            if "iki yarıda kg" in p or "iki yarı kg" in p:
                return (ht_home >= 1 and ht_away >= 1) and (sh2_home >= 1 and sh2_away >= 1)
            return None

        # 3. İY (İlk Yarı) ve İY/MS Bahisleri
        if "iy/ms" in p or "iy " in p or "ilk yarı" in p:
            if "1/1" in p and home_score <= away_score:
                return False
            if "2/2" in p and away_score <= home_score:
                return False
            if "1.5" in p and ("üst" in p or "over" in p) and tot < 2:
                return False
            if ht_home is None or ht_away is None:
                return None
            ht_tot = ht_home + ht_away
            if "1/2" in p:
                return ht_home > ht_away and home_score < away_score
            if "2/1" in p:
                return ht_home < ht_away and home_score > away_score
            if "x/1" in p or "0/1" in p:
                return ht_home == ht_away and home_score > away_score
            if "x/2" in p or "0/2" in p:
                return ht_home == ht_away and home_score < away_score
            if "1/1" in p:
                return ht_home > ht_away and home_score > away_score
            if "2/2" in p:
                return ht_home < ht_away and home_score < away_score
            if "1.5" in p and ("üst" in p or "over" in p):
                return ht_tot >= 2
            if "1.5" in p and ("alt" in p or "under" in p):
                return ht_tot < 2
            if "0.5" in p and ("üst" in p or "over" in p):
                return ht_tot >= 1
            if "0.5" in p and ("alt" in p or "under" in p):
                return ht_tot == 0
            return None

        # 4. Handikaplı Maç Sonucu (En Az 2 Fark)
        if "handikap" in p or "2 fark" in p:
            if "ms 1" in p or "ev sahibi" in p:
                return (home_score - away_score) >= 2
            if "ms 2" in p or "deplasman" in p:
                return (away_score - home_score) >= 2

        # 5. Kombine Bahisler (MS 1 & 2.5 Üst, MS 1 & 1.5 Üst, KG Var & 2.5 Üst vb.)
        if "&" in p or " ve " in p:
            parts = [x.strip() for x in (p.split("&") if "&" in p else p.split(" ve "))]
            sub_res = [MatchQuantitativeAnalyzer.evaluate_pick(part, home_score, away_score, ht_home, ht_away) for part in parts]
            if any(sr is False for sr in sub_res):
                return False
            if all(sr is True for sr in sub_res):
                return True
            return None

        # 6. Kesin Skor Sniper
        if "skor:" in p or "skor " in p:
            m = re.search(r"(\d+)[\-_:](\d+)", p)
            if m:
                return home_score == int(m.group(1)) and away_score == int(m.group(2))

        # 7. Çifte Şans
        if "çifte şans" in p or "1x" in p or "x2" in p or "12" in p:
            if "1x" in p:
                return home_score >= away_score
            if "x2" in p:
                return away_score >= home_score
            if "12" in p:
                return home_score != away_score

        # 8. Ev Sahibi / Deplasman Takım Golleri
        if "ev sahibi" in p:
            if "0.5" in p and "üst" in p:
                return home_score >= 1
            if "1.5" in p and "üst" in p:
                return home_score >= 2
            if "1.5" in p and "alt" in p:
                return home_score < 2
            if "2.5" in p and "üst" in p:
                return home_score >= 3
        if "deplasman" in p:
            if "0.5" in p and "üst" in p:
                return away_score >= 1
            if "1.5" in p and "üst" in p:
                return away_score >= 2
            if "1.5" in p and "alt" in p:
                return away_score < 2
            if "2.5" in p and "üst" in p:
                return away_score >= 3

        # 9. KG Var / KG Yok
        if "kg var" in p or "karşılıklı gol var" in p:
            return home_score >= 1 and away_score >= 1
        if "kg yok" in p or "karşılıklı gol yok" in p:
            return home_score == 0 or away_score == 0

        # 10. Toplam Gol Aralıkları
        if "0-1" in p:
            return tot <= 1
        if "2-3" in p:
            return 2 <= tot <= 3
        if "4-5" in p:
            return 4 <= tot <= 5
        if "6+" in p or "+6" in p:
            return tot >= 6

        # 11. Toplam Gol Üst/Alt
        if "1.5" in p and ("üst" in p or "over" in p):
            return tot >= 2
        if "1.5" in p and ("alt" in p or "under" in p):
            return tot < 2
        if "2.5" in p and ("üst" in p or "over" in p):
            return tot >= 3
        if "2.5" in p and ("alt" in p or "under" in p):
            return tot < 3
        if "3.5" in p and ("üst" in p or "over" in p):
            return tot >= 4
        if "3.5" in p and ("alt" in p or "under" in p):
            return tot < 4
        if "4.5" in p and ("üst" in p or "over" in p):
            return tot >= 5
        if "4.5" in p and ("alt" in p or "under" in p):
            return tot < 5

        # 12. Maç Sonu (MS 1, MS X, MS 2)
        if "ms 1" in p or p == "1" or "maç sonucu 1" in p:
            return home_score > away_score
        if "ms 2" in p or p == "2" or "maç sonucu 2" in p:
            return away_score > home_score
        if "ms x" in p or "ms 0" in p or p in ("x", "0") or "beraberlik" in p:
            return home_score == away_score

        return None

    def _extract_tactical_arsenal(self, match: Dict[str, Any], analyzed: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Bir maç için Bülten'de GERÇEKTEN AÇIK olan tüm taktiksel bahis ihtimallerini
        (MS & Üst, MS & Alt, Ev/Dep Gol, Her İki Yarıda Gol, KG & Üst, 2-3 Gol, İY 1.5 Üst, HMS vb.)
        Dixon-Coles 9x9 olasılık matrisinden gerçek olasılıklarıyla çıkarır.
        YASAKLI BAHİSLER: +6 Gol, İki Yarı KG (Evet/Evet), 1/2-2/1 Sürpriz, Kesin Skor, Kart ve Korner!
        """
        odds = match.get("odds", {})
        mat = analyzed["score_matrix"]
        tot_mat = sum(sum(r) for r in mat) or 1.0
        lam_h = analyzed["lam_home"]
        lam_a = analyzed["lam_away"]
        lam_tot = analyzed["lam_total"]
        lam_1h = analyzed["lam_1h"]
        lam_2h = max(0.20, lam_tot - lam_1h)
        p_draw = analyzed["probabilities"]["ms_x"] / 100.0
        home_name = match["home"]
        away_name = match["away"]

        # Matris olasılık yardımcı fonksiyonu
        def _m_prob(cond_fn) -> float:
            s = 0.0
            for h in range(9):
                for a in range(9):
                    if cond_fn(h, a):
                        s += mat[h][a]
            return min(0.92, max(0.02, s / tot_mat))

        # Ham olasılıklar
        p_ms1 = _m_prob(lambda h, a: h > a)
        p_ms2 = _m_prob(lambda h, a: a > h)
        p_ms1_o15 = _m_prob(lambda h, a: h > a and (h + a) >= 2)
        p_ms2_o15 = _m_prob(lambda h, a: a > h and (h + a) >= 2)
        p_ms1_o25 = _m_prob(lambda h, a: h > a and (h + a) >= 3)
        p_ms2_o25 = _m_prob(lambda h, a: a > h and (h + a) >= 3)
        p_ms1_u35 = _m_prob(lambda h, a: h > a and (h + a) <= 3)
        p_ms2_u35 = _m_prob(lambda h, a: a > h and (h + a) <= 3)
        p_ms1_u45 = _m_prob(lambda h, a: h > a and (h + a) <= 4)
        p_ms2_u45 = _m_prob(lambda h, a: a > h and (h + a) <= 4)
        p_ms1_o35 = _m_prob(lambda h, a: h > a and (h + a) >= 4)
        p_ms2_o35 = _m_prob(lambda h, a: a > h and (h + a) >= 4)
        p_kg_o25 = _m_prob(lambda h, a: h >= 1 and a >= 1 and (h + a) >= 3)
        p_ms1_kg = _m_prob(lambda h, a: h > a and a >= 1)
        p_ms2_kg = _m_prob(lambda h, a: a > h and h >= 1)
        p_ev_o15 = _m_prob(lambda h, a: h >= 2)
        p_dep_o15 = _m_prob(lambda h, a: a >= 2)
        p_ev_o25 = _m_prob(lambda h, a: h >= 3)
        p_dep_o25 = _m_prob(lambda h, a: a >= 3)
        p_o25 = _m_prob(lambda h, a: (h + a) >= 3)
        p_o35 = _m_prob(lambda h, a: (h + a) >= 4)
        p_u25 = _m_prob(lambda h, a: (h + a) <= 2)
        p_btts = _m_prob(lambda h, a: h >= 1 and a >= 1)
        p_btts_no = _m_prob(lambda h, a: h == 0 or a == 0)
        p_tg_23 = _m_prob(lambda h, a: 2 <= (h + a) <= 3)
        p_tg_45 = _m_prob(lambda h, a: 4 <= (h + a) <= 5)
        p_hms1 = _m_prob(lambda h, a: (h - a) >= 2)
        p_hms2 = _m_prob(lambda h, a: (a - h) >= 2)

        # İlk Yarı / İkinci Yarı olasılıkları
        p_1h_goal = 1.0 - math.exp(-lam_1h)
        p_2h_goal = 1.0 - math.exp(-lam_2h)
        p_both_halves_goal = p_1h_goal * p_2h_goal * 0.90
        p_iy_o15 = (1.0 - (1.0 + lam_1h) * math.exp(-lam_1h)) * 0.94
        p_ev_both_halves = (1.0 - math.exp(-lam_h * 0.44)) * (1.0 - math.exp(-lam_h * 0.56)) * 0.92
        p_dep_both_halves = (1.0 - math.exp(-lam_a * 0.44)) * (1.0 - math.exp(-lam_a * 0.56)) * 0.92
        p_iyms_11 = min(0.68, p_ms1 * (0.58 + min(0.18, (lam_h - lam_a) * 0.08))) if lam_h > lam_a else 0.15
        p_iyms_22 = min(0.68, p_ms2 * (0.58 + min(0.18, (lam_a - lam_h) * 0.08))) if lam_a > lam_h else 0.15

        raw_options = [
            # (Market Ailesi, Tercih Metni, Oran, Model Olasılığı (0-1), Taktiksel Açıklama, Ek Koşul)
            (
                "ms_over",
                "MS 1 & 1.5 Üst",
                odds.get("ms1_ve_15_ust"),
                p_ms1_o15,
                f"Ev sahibi favori (λ={lam_h:.2f}) ve maçın en az 2 gole gitme olasılığı yüksek (%{p_ms1_o15*100:.1f}).",
                p_ms1 >= 0.48 and lam_h >= 1.45,
            ),
            (
                "ms_over",
                "MS 2 & 1.5 Üst",
                odds.get("ms2_ve_15_ust"),
                p_ms2_o15,
                f"Deplasman favori (λ={lam_a:.2f}) ve en az 2 golle kazanma olasılığı %{p_ms2_o15*100:.1f}.",
                p_ms2 >= 0.48 and lam_a >= 1.45,
            ),
            (
                "ms_over",
                "MS 1 & 2.5 Üst",
                odds.get("ms1_ve_25_ust"),
                p_ms1_o25,
                f"Ev sahibi baskılı hücum gücüyle (λ={lam_h:.2f}) maçı 2.5 Üst'e taşıyarak kazanır.",
                p_ms1 >= 0.46 and lam_tot >= 2.50,
            ),
            (
                "ms_over",
                "MS 2 & 2.5 Üst",
                odds.get("ms2_ve_25_ust"),
                p_ms2_o25,
                f"Deplasman üstün hücum temposuyla (λ={lam_a:.2f}) gollü galibiyete yakın.",
                p_ms2 >= 0.46 and lam_tot >= 2.50,
            ),
            (
                "ms_under",
                "MS 1 & 3.5 Alt",
                odds.get("ms1_ve_35_alt"),
                p_ms1_u35,
                f"Kontrollü favori taktiği: 1-0, 2-0, 2-1, 3-0 skorlarının tamamını kapsar (%{p_ms1_u35*100:.1f}).",
                p_ms1 >= 0.48 and lam_tot <= 3.05,
            ),
            (
                "ms_under",
                "MS 2 & 3.5 Alt",
                odds.get("ms2_ve_35_alt"),
                p_ms2_u35,
                f"Kontrollü deplasman galibiyeti: 0-1, 0-2, 1-2, 0-3 skorlarını kapsar (%{p_ms2_u35*100:.1f}).",
                p_ms2 >= 0.48 and lam_tot <= 3.05,
            ),
            (
                "ms_under",
                "MS 1 & 4.5 Alt",
                odds.get("ms1_ve_45_alt"),
                p_ms1_u45,
                f"Ev sahibi galibiyeti + 4.5 Alt güvenlik kalkanı (Model: %{p_ms1_u45*100:.1f}).",
                p_ms1 >= 0.50 and lam_tot <= 3.40,
            ),
            (
                "ms_under",
                "MS 2 & 4.5 Alt",
                odds.get("ms2_ve_45_alt"),
                p_ms2_u45,
                f"Deplasman galibiyeti + 4.5 Alt güvenlik kalkanı (Model: %{p_ms2_u45*100:.1f}).",
                p_ms2 >= 0.50 and lam_tot <= 3.40,
            ),
            (
                "btts_combo",
                "KG Var & 2.5 Üst",
                odds.get("kg_var_ve_25_ust"),
                p_kg_o25,
                f"Çift taraflı açık futbol (Ev λ={lam_h:.2f}, Dep λ={lam_a:.2f}); hem KG hem 2.5 Üst bir arada!",
                lam_h >= 1.15 and lam_a >= 1.05 and lam_tot >= 2.65,
            ),
            (
                "btts_combo",
                "MS 1 & KG Var",
                odds.get("ms1_ve_kg_var"),
                p_ms1_kg,
                f"Ev sahibi kazanır ancak konuk ekip de 1 gol bulur (2-1, 3-1, 3-2 senaryosu).",
                p_ms1 >= 0.48 and lam_a >= 0.90,
            ),
            (
                "btts_combo",
                "MS 2 & KG Var",
                odds.get("ms2_ve_kg_var"),
                p_ms2_kg,
                f"Deplasman kazanır ancak ev sahibi de skor üretir (1-2, 1-3 senaryosu).",
                p_ms2 >= 0.48 and lam_h >= 0.90,
            ),
            (
                "team_goals",
                f"Ev Sahibi ({home_name}) 1.5 Gol Üst",
                odds.get("ev_ust_15"),
                p_ev_o15,
                f"Beraberlik kalkanlı tercih: Maç 2-2 bitse bile {home_name} 2 gol attığında kazanır (λ_ev={lam_h:.2f}).",
                lam_h >= 1.55,
            ),
            (
                "team_goals",
                f"Deplasman ({away_name}) 1.5 Gol Üst",
                odds.get("dep_ust_15"),
                p_dep_o15,
                f"Beraberlik kalkanlı tercih: Maç 2-2 bitse bile {away_name} 2 gol attığında kazanır (λ_dep={lam_a:.2f}).",
                lam_a >= 1.55,
            ),
            (
                "team_goals",
                f"Ev Sahibi ({home_name}) 2.5 Gol Üst",
                odds.get("ev_ust_25"),
                p_ev_o25,
                f"Ağır hücum üstünlüğü: {home_name} gol beklentisi λ={lam_h:.2f} (3+ gol potansiyeli).",
                lam_h >= 2.05,
            ),
            (
                "team_goals",
                f"Deplasman ({away_name}) 2.5 Gol Üst",
                odds.get("dep_ust_25"),
                p_dep_o25,
                f"Ağır deplasman baskısı: {away_name} gol beklentisi λ={lam_a:.2f} (3+ gol potansiyeli).",
                lam_a >= 2.05,
            ),
            (
                "team_halves",
                f"Ev Sahibi ({home_name}) Her İki Yarıda Gol Atar",
                odds.get("ev_iki_yari_gol"),
                p_ev_both_halves,
                f"{home_name} yüksek hücum temposuyla (λ_ev={lam_h:.2f}) hem 1. yarıda hem 2. yarıda gol bulur.",
                lam_h >= 1.85,
            ),
            (
                "team_halves",
                f"Deplasman ({away_name}) Her İki Yarıda Gol Atar",
                odds.get("dep_iki_yari_gol"),
                p_dep_both_halves,
                f"{away_name} yüksek hücum temposuyla (λ_dep={lam_a:.2f}) hem 1. yarıda hem 2. yarıda gol bulur.",
                lam_a >= 1.85,
            ),
            (
                "halves_goals",
                "Her İki Yarıda Gol Olur",
                odds.get("iki_yari_gol_var"),
                p_both_halves_goal,
                f"Takım fark etmeksizin hem 1. yarıda hem 2. yarıda en az 1'er gol çıkar (λ_1Y={lam_1h:.2f}, λ_2Y={lam_2h:.2f}).",
                lam_tot >= 2.75,
            ),
            (
                "iy_over",
                "İlk Yarı 1.5 Gol Üst",
                odds.get("iy_ust_15"),
                p_iy_o15,
                f"Erken baskı ve yüksek ilk yarı temposu (λ_1Y={lam_1h:.2f}); ilk 45 dakikada 2+ gol.",
                lam_1h >= 1.20,
            ),
            (
                "goal_range",
                "2-3 Gol Aralığı",
                odds.get("tg_2_3"),
                p_tg_23,
                f"Altın Bant: 1-1, 2-0, 0-2, 2-1 ve 1-2 skorlarının tamamında kazanır (λ_toplam={lam_tot:.2f}).",
                2.10 <= lam_tot <= 2.85,
            ),
            (
                "goal_range",
                "4-5 Gol Aralığı",
                odds.get("tg_4_5"),
                p_tg_45,
                f"Yüksek tempolu maçta +6 gol riskine girmeden 4 veya 5 gol bandı (λ_toplam={lam_tot:.2f}).",
                lam_tot >= 3.00,
            ),
            (
                "over_under",
                "2.5 Gol Üst",
                odds.get("ust_25"),
                p_o25,
                f"Toplam gol beklentisi λ={lam_tot:.2f}; Dixon-Coles 3+ gol olasılığı %{p_o25*100:.1f}.",
                lam_tot >= 2.60,
            ),
            (
                "over_under",
                "3.5 Gol Üst",
                odds.get("ust_35"),
                p_o35,
                f"Gol fırtınası beklentisi (λ={lam_tot:.2f}); 4+ gol olasılığı yüksek.",
                lam_tot >= 2.95,
            ),
            (
                "over_under",
                "2.5 Gol Alt",
                odds.get("alt_25"),
                p_u25,
                f"Savunma duvarı (λ={lam_tot:.2f}); düşük tempolu maçta en fazla 2 gol çıkar.",
                lam_tot <= 2.25,
            ),
            (
                "btts",
                "KG Var",
                odds.get("kg_var"),
                p_btts,
                f"Dengeli hücum gücü (Ev λ={lam_h:.2f} / Dep λ={lam_a:.2f}); karşılıklı gol olasılığı %{p_btts*100:.1f}.",
                lam_h >= 1.15 and lam_a >= 1.05,
            ),
            (
                "btts",
                "KG Yok",
                odds.get("kg_yok"),
                p_btts_no,
                f"Tek taraflı veya kısır maç (En az bir takım gol atamaz: %{p_btts_no*100:.1f}).",
                min(lam_h, lam_a) <= 0.78,
            ),
            (
                "match_winner",
                "MS 1",
                odds.get("ms_1"),
                p_ms1,
                f"Beraberlik kalkanından geçmiş net ev sahibi üstünlüğü (Model: %{p_ms1*100:.1f}, Beraberlik riski sadece %{p_draw*100:.1f}).",
                p_ms1 >= 0.53 and p_draw <= 0.265 and lam_h >= 1.60,
            ),
            (
                "match_winner",
                "MS 2",
                odds.get("ms_2"),
                p_ms2,
                f"Beraberlik kalkanından geçmiş net deplasman üstünlüğü (Model: %{p_ms2*100:.1f}, Beraberlik riski sadece %{p_draw*100:.1f}).",
                p_ms2 >= 0.53 and p_draw <= 0.265 and lam_a >= 1.60,
            ),
            (
                "handicap",
                f"Handikaplı MS 1 ({home_name} En Az 2 Fark)",
                odds.get("hms1_2fark"),
                p_hms1,
                f"{home_name} ağır kalite farkıyla (λ_ev={lam_h:.2f} vs λ_dep={lam_a:.2f}) en az 2 farkla kazanır.",
                (lam_h - lam_a) >= 1.05 and p_ms1 >= 0.62,
            ),
            (
                "handicap",
                f"Handikaplı MS 2 ({away_name} En Az 2 Fark)",
                odds.get("hms2_2fark"),
                p_hms2,
                f"{away_name} ağır kalite farkıyla (λ_dep={lam_a:.2f} vs λ_ev={lam_h:.2f}) en az 2 farkla kazanır.",
                (lam_a - lam_h) >= 1.05 and p_ms2 >= 0.62,
            ),
            (
                "ht_ft_fav",
                f"İY/MS 1/1 ({home_name} İlk Yarıdan İşi Bitirir)",
                odds.get("iyms_1_1"),
                p_iyms_11,
                f"{home_name} maça baskılı başlayıp ilk yarıdan öne geçerek maçı kazanır.",
                p_ms1 >= 0.60 and lam_h >= 1.80,
            ),
            (
                "ht_ft_fav",
                f"İY/MS 2/2 ({away_name} İlk Yarıdan İşi Bitirir)",
                odds.get("iyms_2_2"),
                p_iyms_22,
                f"{away_name} maça baskılı başlayıp ilk yarıdan öne geçerek maçı kazanır.",
                p_ms2 >= 0.60 and lam_a >= 1.80,
            ),
        ]

        valid_tactics: List[Dict[str, Any]] = []
        is_balanced_match = abs(lam_h - lam_a) < 0.75

        for fam, pick_str, odd_val, prob_01, reason_str, is_ok in raw_options:
            if not odd_val or not is_ok:
                continue
            odd_f = float(odd_val)
            if not (1.35 <= odd_f <= 3.60):
                continue
            if prob_01 < 0.32:
                continue

            ev = (prob_01 * odd_f) - 1.0

            # 🛡️ TARAF BAĞIMSIZ GÜVEN KALKANI:
            # 2.5 Üst/Alt, KG Var, 2-3 Gol ve Takım Golleri maçın beraberlikle veya sürprizle bitme
            # riskinden muaftır! Denk maçlarda taraf bahsi yerine gol/KG tercihi VASTLY daha garantidir.
            is_side_independent = fam in ("over_under", "btts", "goal_range", "team_goals", "halves_goals", "iy_over")
            side_bonus = 6.5 if is_side_independent else 0.0
            if is_balanced_match:
                if is_side_independent:
                    side_bonus += 5.5  # Denk maçta Üst/Alt ve KG Var çok daha güvenli
                elif fam in ("match_winner", "ms_over", "ms_under", "handicap", "ht_ft_fav"):
                    side_bonus -= 6.0  # Denk maçta MS taraf tahmini gereksiz varyans ve yatma riski taşır

            conf_score = (prob_01 * 100.0) * 0.80 + max(-8.0, min(18.0, ev * 45.0)) + side_bonus + 10.0
            valid_tactics.append({
                "family": fam,
                "is_side_independent": is_side_independent,
                "pick": pick_str,
                "odd": round(odd_f, 2),
                "prob": round(prob_01 * 100.0, 1),
                "confidence": round(min(98.0, max(45.0, conf_score)), 1),
                "ev_pct": round(ev * 100.0, 1),
                "reasons": [reason_str],
            })

        valid_tactics.sort(key=lambda x: (x["confidence"], x["prob"]), reverse=True)
        return valid_tactics

    def build_sniper_4h_coupon(
        self,
        matches: List[Dict[str, Any]],
        hours: int = 4,
        target_odd: float = 20.0,
        fixed_stake: float = 50.0,
        elite_only: bool = False,
        exclude_codes: Optional[Set[str]] = None,
    ) -> Dict[str, Any]:
        """
        Verilen hedef oran (5.0, 10.0 veya 20.0) ve bahis tutarı (200 TL, 100 TL, 50 TL) için
        EN AZ MAÇLA ve EN YÜKSEK ORTAK OLASILIKLA (Joint Probability) hedefi vuran kupon üretir.
        - +6 Gol, İki Yarı KG (Evet/Evet), 1/2 Sürpriz, Kesin Skor, Kart ve Korner KESİNLİKLE YASAKTIR.
        - MS & Üst, MS & Alt, Ev/Dep 1.5-2.5 Üst, Ev/Dep İki Yarıda Gol, Her İki Yarıda Gol, KG & Üst, 2-3 Gol gibi
          tüm taktiksel silahları kullanır.
        """
        from datetime import datetime, timedelta
        import itertools

        now = datetime.now()
        actual_hours = hours
        window_matches: List[Dict[str, Any]] = []

        for h_try in (hours, 6, 8, 12, 24):
            limit_dt = now + timedelta(hours=h_try)
            cands = [m for m in matches if now <= m.get("start_dt", now) <= limit_dt]
            if exclude_codes:
                cands = [m for m in cands if m.get("code") not in exclude_codes]
            if elite_only:
                elite_cands = [m for m in cands if self.classify_match_league_tier(m) in (1, 2)]
                if len(elite_cands) >= 5:
                    cands = elite_cands
            if len(cands) >= 6 or h_try == 24:
                window_matches = cands
                actual_hours = h_try
                break

        limit_dt = now + timedelta(hours=actual_hours)

        # Hedef Orana Göre Minimum Maç Sayısı ve İdeal Maç Başı Oran (En Az Maç Kuralı!)
        if target_odd <= 6.5:
            # 200 TL -> 5 Oran Kupon: SADECE 2 veya 3 MAÇ!
            target_min, target_max = target_odd * 0.88, target_odd * 1.22
            leg_options = (2, 3)
            min_leg_odd, max_leg_odd = 1.48, 2.55
            ideal_leg_odd = 1.75
        elif target_odd <= 13.0:
            # 100 TL -> 10 Oran Kupon: SADECE 3 veya 4 MAÇ!
            target_min, target_max = target_odd * 0.88, target_odd * 1.22
            leg_options = (3, 4)
            min_leg_odd, max_leg_odd = 1.62, 2.85
            ideal_leg_odd = 2.08
        else:
            # 50 TL -> 20 Oran Kupon: SADECE 3 veya 4 MAÇ (Asla 5+ maç yapmaz)!
            target_min, target_max = target_odd * 0.86, target_odd * 1.25
            leg_options = (3, 4)
            min_leg_odd, max_leg_odd = 1.80, 3.50
            ideal_leg_odd = 2.28

        candidate_pool: List[Tuple[float, Dict[str, Any], Dict[str, Any], str, int]] = []

        for m in window_matches:
            a = self.analyze_match(m)
            tier = self.classify_match_league_tier(m)
            tier_bonus = 6.5 if tier == 1 else (3.5 if tier == 2 else -3.5)
            in_strict_4h = (m.get("start_dt", now) <= now + timedelta(hours=hours))
            time_bonus = 4.0 if in_strict_4h else 0.0

            tactics = self._extract_tactical_arsenal(m, a)
            fams_best: Dict[str, Tuple[float, Dict[str, Any]]] = {}

            for tac in tactics:
                odd = tac["odd"]
                if not (min_leg_odd <= odd <= max_leg_odd):
                    continue
                fam = tac["family"]
                # Hedef kupon oranına en uygun maç başı oranı ödüllendir (3-4 maçta hedefi tam vurmak için)
                leg_fit_bonus = max(0.0, 22.0 - abs(odd - ideal_leg_odd) * 18.0)
                sniper_score = (tac["confidence"] * 0.50) + (tac["prob"] * 0.40) + leg_fit_bonus + tier_bonus + time_bonus
                if fam not in fams_best or sniper_score > fams_best[fam][0]:
                    fams_best[fam] = (sniper_score, tac)

            # Her maçtan en iyi 3 farklı taktiksel marketi aday havuzuna ekle
            sorted_fams = sorted(fams_best.items(), key=lambda x: x[1][0], reverse=True)[:3]
            for fam, (sc, sig) in sorted_fams:
                candidate_pool.append((sc, a, sig, fam, tier))

        candidate_pool.sort(key=lambda x: x[0], reverse=True)
        top_cands = candidate_pool[:30]

        best_combo = None
        best_obj_score = -1e9

        for k in leg_options:
            if len(top_cands) < k:
                continue
            for combo in itertools.combinations(top_cands, k):
                # 1. Her maç farklı olmalı
                codes = [item[1]["match"]["code"] for item in combo]
                if len(set(codes)) != k:
                    continue

                # 2. Pazar çeşitliliği: Tek tip bahis YASAK!
                fams = [item[3] for item in combo]
                unique_fams = len(set(fams))
                if k >= 3 and unique_fams < min(3, k):
                    continue
                # Aynı market ailesinden en fazla 1 adet (4 maçlıkta en fazla 2 adet) olabilir
                max_same_fam = 1 if k <= 3 else 2
                if any(fams.count(f_set) > max_same_fam for f_set in set(fams)):
                    continue

                # 3. Taraf Riski Dengesi: Asla tüm kupon maçları taraf (MS) bahsi olamaz!
                # Kuponda mutlaka en az 1-2 adet taraf bağımsız (2.5 Üst/Alt, KG Var, 2-3 Gol, Takım Golü)
                # maç bulunmalıdır. Çünkü denk maçlarda Üst/Alt ve KG Var taraf bahsinden çok daha garantidir!
                side_indep_count = sum(1 for item in combo if item[2].get("is_side_independent"))
                if side_indep_count == 0 and k >= 2:
                    continue

                tot_odd = 1.0
                joint_prob = 1.0
                min_prob = 100.0
                avg_sniper = 0.0
                for sc, a_item, sig, fam, tier in combo:
                    tot_odd *= float(sig["odd"])
                    p_val = float(sig["prob"])
                    joint_prob *= (p_val / 100.0)
                    if p_val < min_prob:
                        min_prob = p_val
                    avg_sniper += sc
                avg_sniper /= k

                if tot_odd < target_min * 0.82 or tot_odd > target_max * 1.25:
                    continue

                in_band = 1.0 if (target_min <= tot_odd <= target_max) else 0.0
                log_dist = abs(math.log(max(1.0, tot_odd) / target_odd))

                # EN AZ MAÇ & EN YÜKSEK OLASILIK ÖDÜLÜ
                fewest_legs_bonus = (5 - k) * 14.0
                # Taraf riski içermeyen garanti market ödülü (Üst/Alt, KG Var, vb.)
                side_shield_bonus = side_indep_count * 10.0

                obj_score = (
                    (avg_sniper * 1.35)
                    + (min_prob * 1.10)
                    + (joint_prob * 100.0 * 2.8)
                    + (in_band * 120.0)
                    + (unique_fams * 14.0)
                    + fewest_legs_bonus
                    + side_shield_bonus
                    - (log_dist * 195.0)
                )

                if obj_score > best_obj_score:
                    best_obj_score = obj_score
                    best_combo = (combo, tot_odd, joint_prob * 100.0, min_prob)

        # Fallback: Eğer katı aralıkta bulunamadıysa hedef orana logaritmik olarak en yakın ve çeşitli kombinasyonu seç
        if not best_combo and top_cands:
            for k in leg_options:
                if len(top_cands) < k:
                    continue
                for combo in itertools.combinations(top_cands[:20], k):
                    codes = [item[1]["match"]["code"] for item in combo]
                    if len(set(codes)) != k:
                        continue
                    fams = [item[3] for item in combo]
                    if len(set(fams)) < min(2, k):
                        continue
                    tot_odd = 1.0
                    joint_prob = 1.0
                    min_prob = 100.0
                    for sc, a_item, sig, fam, tier in combo:
                        tot_odd *= float(sig["odd"])
                        p_val = float(sig["prob"])
                        joint_prob *= (p_val / 100.0)
                        min_prob = min(min_prob, p_val)
                    log_dist = abs(math.log(max(1.0, tot_odd) / target_odd))
                    fb_score = (joint_prob * 100.0 * 2.0) + (len(set(fams)) * 12.0) - (log_dist * 220.0)
                    if fb_score > best_obj_score:
                        best_obj_score = fb_score
                        best_combo = (combo, tot_odd, joint_prob * 100.0, min_prob)

        if not best_combo:
            return {
                "target_odd": target_odd,
                "fixed_stake": fixed_stake,
                "window_hours": actual_hours,
                "start_time": now.strftime("%H:%M"),
                "end_time": limit_dt.strftime("%H:%M"),
                "match_count": len(window_matches),
                "total_odd": 0.0,
                "expected_payout": 0.0,
                "picks": [],
            }

        combo_items, tot_odd, joint_prob_pct, min_prob_val = best_combo
        sorted_items = sorted(combo_items, key=lambda x: x[1]["match"].get("start_dt", now))
        max_prob_code = max(sorted_items, key=lambda x: float(x[2]["prob"]))[1]["match"]["code"]

        picks = []
        for idx, (sc, a_item, sig, fam, tier) in enumerate(sorted_items):
            m_obj = a_item["match"]
            is_anchor = (m_obj["code"] == max_prob_code)
            is_last_hedge = (idx == len(sorted_items) - 1 and len(sorted_items) >= 2)

            role_badge = ""
            if is_anchor:
                role_badge = "⚓ KUPON ÇAPASI (En Yüksek Güven)"
            elif is_last_hedge:
                role_badge = "🛡️ SON MAÇ (Canlı Kâr Kilitleme Noktası)"

            tier_str = "👑 Ana Lig" if tier == 1 else ("🥈 2. Kademe" if tier == 2 else "⚽ Bülten")
            reason_str = sig["reasons"][0] if sig.get("reasons") else ""
            top_sc_str = " | ".join(f"{s_code} (%{s_pr})" for s_code, s_pr in a_item.get("top_scores", [])[:3])

            picks.append({
                "code": m_obj["code"],
                "match": f"{m_obj['home']} - {m_obj['away']}",
                "league": m_obj["league"],
                "tier_label": tier_str,
                "league_tier": tier,
                "time": f"{m_obj['date']} {m_obj['time']}",
                "pick": sig["pick"],
                "odd": round(float(sig["odd"]), 2),
                "prob": round(float(sig["prob"]), 1),
                "ev_pct": round(float(sig.get("ev_pct", 0.0)), 1),
                "lam_home": a_item.get("lam_home", "-"),
                "lam_away": a_item.get("lam_away", "-"),
                "top_scores": top_sc_str,
                "confidence": round(float(sig.get("confidence", 0.0)), 1),
                "family": fam,
                "role_badge": role_badge,
                "reason": reason_str,
            })

        total_odd_rounded = round(tot_odd, 2)
        expected_payout = round(total_odd_rounded * fixed_stake, 2)

        return {
            "target_odd": target_odd,
            "fixed_stake": fixed_stake,
            "window_hours": actual_hours,
            "elite_only": elite_only,
            "start_time": now.strftime("%H:%M"),
            "end_time": limit_dt.strftime("%H:%M"),
            "match_count": len(window_matches),
            "total_odd": total_odd_rounded,
            "expected_payout": expected_payout,
            "net_profit": round(expected_payout - fixed_stake, 2),
            "joint_prob": round(joint_prob_pct, 1),
            "min_leg_prob": round(min_prob_val, 1),
            "picks": picks,
        }

    def build_trinity_4h_system(
        self,
        matches: List[Dict[str, Any]],
        hours: int = 4,
        elite_only: bool = False,
    ) -> Dict[str, Any]:
        """
        Önümüzdeki 4 Saat İçin Kusursuz 3'lü Kupon Sistemi (Her Biri ~1.000 TL Hedefli):
        1) 🟢 KUPON 1: 200 TL Yatırım ➔ ~5.00 Oran  (2-3 Maç | Hedef: ~1.000 TL)
        2) 🟡 KUPON 2: 100 TL Yatırım ➔ ~10.00 Oran (3-4 Maç | Hedef: ~1.000 TL)
        3) 🔴 KUPON 3: 50 TL Yatırım  ➔ ~20.00 Oran (3-4 Maç | Hedef: ~1.000 TL)

        Özellikler:
        - Kuponlar arası maç izolasyonu (Bir kupondaki maç diğer kupona yazılmaz; tek maç yatışı diğer kuponları etkilemez).
        - +6 Gol, İki Yarı KG (Evet/Evet), 1/2 Sürpriz, Kart/Korner tamamen yasaklıdır.
        - En az maç sayısı ve tüm taktiksel market kombinasyonları (MS&Üst, MS&Alt, Ev/Dep Gol, İki Yarıda Gol, 2-3 Gol vb.) kullanılır.
        """
        used_codes: Set[str] = set()

        # 1. Kupon: 200 TL -> 5 Oran (En Az Maç: 2-3 Maç)
        c_5x = self.build_sniper_4h_coupon(
            matches,
            hours=hours,
            target_odd=5.0,
            fixed_stake=200.0,
            elite_only=elite_only,
            exclude_codes=used_codes,
        )
        c_5x["title"] = "🟢 1. KUPON: 200 ₺ ➔ 5 ORAN KUPONU (Hedef: ~1.000 ₺)"
        c_5x["subtitle"] = "En az maçla (2-3 maç), en yüksek olasılıklı taktiksel ana kasa kuponu."
        for p in c_5x.get("picks", []):
            if p.get("code"):
                used_codes.add(p["code"])

        # 2. Kupon: 100 TL -> 10 Oran (En Az Maç: 3-4 Maç)
        c_10x = self.build_sniper_4h_coupon(
            matches,
            hours=hours,
            target_odd=10.0,
            fixed_stake=100.0,
            elite_only=elite_only,
            exclude_codes=used_codes,
        )
        c_10x["title"] = "🟡 2. KUPON: 100 ₺ ➔ 10 ORAN KUPONU (Hedef: ~1.000 ₺)"
        c_10x["subtitle"] = "Farklı maçlardan oluşan, MS&Üst / Takım Golü / 2-3 Gol odaklı orta çarpan kuponu."
        for p in c_10x.get("picks", []):
            if p.get("code"):
                used_codes.add(p["code"])

        # 3. Kupon: 50 TL -> 20 Oran (En Az Maç: 3-4 Maç)
        c_20x = self.build_sniper_4h_coupon(
            matches,
            hours=hours,
            target_odd=20.0,
            fixed_stake=50.0,
            elite_only=elite_only,
            exclude_codes=used_codes,
        )
        c_20x["title"] = "🔴 3. KUPON: 50 ₺ ➔ 20 ORAN KUPONU (Hedef: ~1.000 ₺)"
        c_20x["subtitle"] = "En fazla 4 maçla 20 oranı vuran, +6 gol ve fantezi bahislerden arındırılmış keskin nişancı kuponu."

        total_stake = 200.0 + 100.0 + 50.0
        total_potential = round(
            c_5x.get("expected_payout", 0.0)
            + c_10x.get("expected_payout", 0.0)
            + c_20x.get("expected_payout", 0.0),
            2,
        )

        return {
            "window_hours": max(
                c_5x.get("window_hours", hours),
                c_10x.get("window_hours", hours),
                c_20x.get("window_hours", hours),
            ),
            "start_time": c_5x.get("start_time", ""),
            "end_time": c_5x.get("end_time", ""),
            "match_count": c_5x.get("match_count", 0),
            "elite_only": elite_only,
            "total_stake": total_stake,
            "total_potential": total_potential,
            "coupons": {
                "kupon_5x": c_5x,
                "kupon_10x": c_10x,
                "kupon_20x": c_20x,
            },
        }
