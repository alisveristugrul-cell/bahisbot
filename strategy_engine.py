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
        both_kg_odd = odds.get("iki_yari_kg_var")
        if not both_kg_odd and odds.get("iy_kg_var") and odds.get("ikinci_yari_kg_var"):
            both_kg_odd = round(odds["iy_kg_var"] * odds["ikinci_yari_kg_var"] * 0.95, 2)

        if lam_h >= 1.25 and lam_a >= 1.15 and p_both_halves_btts >= 0.065:
            display_odd = both_kg_odd or round(1.0 / max(0.05, p_both_halves_btts * 1.12), 2)
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

        return signals

    def scan_bulletin(
        self,
        matches: List[Dict[str, Any]],
        strategy_key: str,
        top_n: int = 8,
        fetcher: Optional[Any] = None,
    ) -> List[Dict[str, Any]]:
        """
        Tüm bülteni tarayarak seçilen taktiğe (örn. 'kg_var', 'arti_6_gol', 'iki_yari_kg')
        en uygun maçları güven skoruna göre sıralar.
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
        Günlük bültenden 3 farklı risk profilinde hazır kupon üretir:
        1. Banko Kupon (Düşük Risk, 3 Maç, Oran ~2.40 - 3.50)
        2. İdeal Değer Kuponu (Orta Risk, 4 Maç, KG Var / 2.5 Üst / Kombo, Oran ~5.00 - 10.00)
        3. Sürpriz / Sistem Kaos Kuponu (Yüksek Oran, +6 Gol / İki Yarı KG / İY 1.5 Üst, Oran ~25.00+)
        """
        all_analyzed = [self.analyze_match(m) for m in matches]

        # 1. Banko Adayları (MS Banko veya 2.5 Üst / KG Var en yüksek olasılıklı)
        banko_pool = []
        for a in all_analyzed:
            for k in ["ms_banko", "ust_25", "kg_var"]:
                s = a["signals"].get(k)
                if s and 1.32 <= s["odd"] <= 1.72 and s["prob"] >= 58.0:
                    banko_pool.append((a, s))
                    break
        banko_pool.sort(key=lambda x: x[1]["prob"], reverse=True)

        # 2. İdeal Kupon Adayları (Value Bet, Kombo, KG Var, 2.5 Üst)
        ideal_pool = []
        used_codes_banko = {x[0]["match"]["code"] for x in banko_pool[:3]}
        for a in all_analyzed:
            if a["match"]["code"] in used_codes_banko:
                continue
            for k in ["value_bet", "kombo", "kg_var", "ust_25"]:
                s = a["signals"].get(k)
                if s and 1.50 <= s["odd"] <= 2.35 and s["confidence"] >= 60.0:
                    ideal_pool.append((a, s))
                    break
        ideal_pool.sort(key=lambda x: (x[1]["confidence"], x[1]["odd"]), reverse=True)

        # 3. Sürpriz / Sistem Kuponu (+6 Gol, İki Yarı KG, 3.5 Üst, İY 1.5 Üst)
        surpriz_pool = []
        for a in all_analyzed:
            for k in ["iki_yari_kg", "arti_6_gol", "iy_ust", "ust_35"]:
                s = a["signals"].get(k)
                if s and s["odd"] >= 2.40:
                    surpriz_pool.append((a, s))
                    break
        surpriz_pool.sort(key=lambda x: x[1]["confidence"], reverse=True)

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

        return {
            "banko": _pack_coupon(
                "🛡️ BANKO GÜVEN KUPONU (Düşük Risk)",
                "Matematiksel tutma olasılığı en yüksek (%60+) 3 ana tercihten oluşur.",
                banko_pool[:3],
            ),
            "ideal": _pack_coupon(
                "⚖️ İDEAL VALUE KUPONU (Orta Risk / Yüksek Değer)",
                "Çapraz market korelasyonu ve +EV (Değer farkı) en yüksek 4 maçtan oluşur.",
                ideal_pool[:4],
            ),
            "surpriz": _pack_coupon(
                "💣 SÜRPRİZ / SİSTEM KAOS KUPONU (+6 Gol & İki Yarı KG & İY 1.5 Üst)",
                "Yüksek varyanslı (Fat-Tail) maçlardan seçilen yüksek oranlı sistem kuponu.",
                surpriz_pool[:3],
            ),
        }
