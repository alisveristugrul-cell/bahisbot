#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Global İddaa & Bahis Tahmin Telegram Botu + Terminal Analiz Aracı
Komutlar:
  /start, /menu       -> İnteraktif Butonlu Ana Menü
  /kg                 -> 🎯 KG Var (BTTS Sniper) Taktiği Tahminleri
  /ms                 -> 🏆 MS (Maç Sonucu) Favori & Value Tahminleri
  /ust25              -> 🔥 2.5 Üst Gol Taktiği Tahminleri
  /ust35              -> 🚀 3.5 Üst Gol Fırtınası Tahminleri
  /arti6              -> 💣 +6 Gol (6+ Gol) Ultra Varyans Taktiği
  /ikiyarikg          -> ⚡ İki Yarıda da KG Var (1Y Evet / 2Y Evet) Taktiği
  /iyust              -> ⏱️ İlk Yarı 1.5 Üst Erken Baskı Taktiği
  /kombo              -> 💎 KG & 2.5 Üst / MS & 2.5 Üst Kombo Taktiği
  /korner             -> 📐 Korner 9.5 / 10.5 Üst Kanat Baskısı Taktiği
  /value              -> 🧠 Sharp Value Bet (+EV & Kelly Kriteri) Fırsatları
  /kupon              -> 🎫 Günün Hazır Kuponları (Banko, İdeal, Sürpriz/Sistem)
  /analiz <takım>     -> 🔍 Belirli Bir Maçın / Takımın X-Ray Röntgeni (Örn: /analiz Chelsea)
  /stratejiler        -> 📚 Botun Kullandığı Global Matematiksel Modellerin Açıklaması
  /oto_ac, /oto_kapat -> 🔔 Yüksek Güvenli Fırsatlar İçin Otomatik Telegram Bildirimi
  /yenile             -> 🔄 Bülteni ve Oranları Canlı Yenile
"""

import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from data_fetcher import GlobalMatchDataFetcher, _normalize_team_name
from strategy_engine import MatchQuantitativeAnalyzer


def _safe_html(text: Any) -> str:
    """Telegram HTML modunda parse hatası vermemesi için özel karakterleri güvenle kaçırır."""
    if text is None:
        return ""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")
SUBSCRIBERS_PATH = os.path.join(CACHE_DIR, "subscribers.json")
PREDICTIONS_PATH = os.path.join(CACHE_DIR, "predictions_record.json")
VIP_CHANNEL_PATH = os.path.join(CACHE_DIR, "vip_channel.txt")


def load_bot_token() -> str:
    """Önce ortam değişkeninden (TELEGRAM_BOT_TOKEN), yoksa config.json'dan token okur."""
    env_tok = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if env_tok:
        return env_tok
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                return str(cfg.get("telegram_bot_token", "")).strip()
        except Exception:
            pass
    return ""


class PredictionTracker:
    """Botun önerdiği kupon ve maçları kaydedip sonuçlarını otomatik puanlayan takip motoru."""

    def __init__(self):
        self.path = PREDICTIONS_PATH
        self._records: List[Dict[str, Any]] = self._load()

    def _load(self) -> List[Dict[str, Any]]:
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return []

    def _save(self) -> None:
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self._records[-500:], f, ensure_ascii=False)
        except Exception:
            pass

    def record_coupon(self, coupon_title: str, picks: List[Dict[str, Any]], total_odd: Optional[float] = None) -> None:
        """Kuponu ve içerdiği tüm maçları tek bir kupon oturumu (coupon_id) altında kaydeder."""
        if not picks:
            return
        now_ts = time.time()
        c_odd = total_odd
        if c_odd is None:
            c_odd = 1.0
            for p in picks:
                c_odd *= float(p.get("odd", 1.0))
            c_odd = round(c_odd, 2)

        matches_str = "_".join(sorted(p.get("match", "") for p in picks))
        c_id = f"c_{abs(hash(coupon_title + matches_str)) % 1000000}"

        existing_ids = {r.get("id") for r in self._records}
        for p in picks:
            p_id = f"{c_id}__{p['match']}__{p['pick']}"
            if p_id not in existing_ids:
                self._records.append({
                    "id": p_id,
                    "coupon_id": c_id,
                    "coupon": coupon_title,
                    "coupon_odd": c_odd,
                    "match": p["match"],
                    "time": p.get("time", ""),
                    "pick": p["pick"],
                    "odd": float(p.get("odd", 1.0)),
                    "prob": float(p.get("prob", 50.0)),
                    "recorded_at": now_ts,
                    "status": "PENDING",
                    "final_score": None,
                })
                existing_ids.add(p_id)
        self._save()

    def record_picks(self, coupon_title: str, picks: List[Dict[str, Any]]) -> None:
        """Geriye dönük uyumluluk için alias."""
        self.record_coupon(coupon_title, picks)

    def update_with_results(self, finished_map: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        """Bitmiş maç skorlarıyla bekleyen kupon tahminlerini puanlar ve hem Kupon ROE hem Tekli ROI üretir."""
        for r in self._records:
            if r["status"] != "PENDING":
                continue
            m_str = r["match"]
            parts = m_str.split(" - ", 1) if " - " in m_str else m_str.split(" vs ", 1)
            if len(parts) < 2:
                continue
            h_norm = _normalize_team_name(parts[0])
            a_norm = _normalize_team_name(parts[1])

            fin = (
                finished_map.get(f"{h_norm}__{a_norm}")
                or finished_map.get(h_norm)
                or finished_map.get(a_norm)
            )
            if fin and fin.get("home_goals") is not None and fin.get("away_goals") is not None:
                hg, ag = fin["home_goals"], fin["away_goals"]
                ht_h, ht_a = fin.get("ht_home_goals"), fin.get("ht_away_goals")
                is_win = MatchQuantitativeAnalyzer.evaluate_pick(r["pick"], hg, ag, ht_h, ht_a)
                if is_win is True:
                    r["status"] = "WON"
                elif is_win is False:
                    r["status"] = "LOST"
                else:
                    # Kart, korner, ilk yarı detayı canlı skor özetinde yoksa unverified olarak işaretle (Asla yanlışlıkla LOST sayılmaz)
                    r["status"] = "UNVERIFIED"
                r["final_score"] = f"{hg}-{ag}"

        self._save()

        # 1. Kupon Bazlı Gruplama & Kupon ROE (Return on Equity)
        coupons_dict: Dict[str, Dict[str, Any]] = {}
        for r in self._records:
            cid = r.get("coupon_id") or r.get("coupon", "legacy")
            if cid not in coupons_dict:
                coupons_dict[cid] = {
                    "id": cid,
                    "title": r.get("coupon", "Kupon"),
                    "odd": float(r.get("coupon_odd", 1.0)),
                    "picks": [],
                }
            coupons_dict[cid]["picks"].append(r)

        evaluated_coupons = []
        won_coupons = []
        lost_coupons = []
        pending_coupons = []
        unverified_coupons = []

        for cid, c in coupons_dict.items():
            statuses = [p["status"] for p in c["picks"]]
            if any(s == "PENDING" for s in statuses):
                pending_coupons.append(c)
            elif any(s == "LOST" for s in statuses):
                lost_coupons.append(c)
                evaluated_coupons.append(c)
            elif all(s == "WON" for s in statuses):
                won_coupons.append(c)
                evaluated_coupons.append(c)
            else:
                unverified_coupons.append(c)

        coupon_staked = len(evaluated_coupons) * 100
        coupon_returned = sum(c["odd"] * 100 for c in won_coupons)
        coupon_roe_pct = ((coupon_returned - coupon_staked) / coupon_staked * 100) if coupon_staked > 0 else 0.0
        coupon_win_rate = (len(won_coupons) / len(evaluated_coupons) * 100) if evaluated_coupons else 0.0

        # 2. Tekil Tercih Başarısı (Pick Win Rate & Single ROI)
        evaluated_picks = [r for r in self._records if r["status"] in ("WON", "LOST")]
        won_picks = [r for r in evaluated_picks if r["status"] == "WON"]
        lost_picks = [r for r in evaluated_picks if r["status"] == "LOST"]
        unverified_picks = [r for r in self._records if r["status"] == "UNVERIFIED"]
        pending_picks = [r for r in self._records if r["status"] == "PENDING"]

        pick_win_rate = (len(won_picks) / len(evaluated_picks) * 100) if evaluated_picks else 0.0
        pick_staked = len(evaluated_picks) * 100
        pick_returned = sum(r["odd"] * 100 for r in won_picks)
        pick_roi_pct = ((pick_returned - pick_staked) / pick_staked * 100) if pick_staked > 0 else 0.0

        return {
            "coupon_total": len(evaluated_coupons),
            "coupon_won": len(won_coupons),
            "coupon_lost": len(lost_coupons),
            "coupon_pending": len(pending_coupons),
            "coupon_win_rate": round(coupon_win_rate, 1),
            "coupon_roe_pct": round(coupon_roe_pct, 1),
            "pick_total": len(evaluated_picks),
            "pick_won": len(won_picks),
            "pick_lost": len(lost_picks),
            "pick_unverified": len(unverified_picks),
            "pick_pending": len(pending_picks),
            "pick_win_rate": round(pick_win_rate, 1),
            "pick_roi_pct": round(pick_roi_pct, 1),
            "recent": evaluated_picks[-6:],
        }


class BettingBotFormatter:
    """Tahminleri, maç analizlerini ve kuponları Telegram/Terminal için şık formatta üretir."""

    def __init__(self, fetcher: GlobalMatchDataFetcher, analyzer: MatchQuantitativeAnalyzer):
        self.fetcher = fetcher
        self.analyzer = analyzer
        self.tracker = PredictionTracker()
        self._matches_cache: List[Dict[str, Any]] = []
        self._last_load_ts: float = 0.0
        self._coupons_cache: Optional[Dict[str, Any]] = None
        self._last_coupons_ts: float = 0.0

    def get_matches(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        now = time.time()
        if force_refresh or not self._matches_cache or (now - self._last_load_ts > 600):
            if force_refresh:
                self.fetcher.cache_ttl = 0
            self._matches_cache = self.fetcher.fetch_matches(max_matches=550)
            self.fetcher.cache_ttl = 600
            self._last_load_ts = now
        return self._matches_cache

    def get_smart_coupons(self, force_refresh: bool = False) -> Dict[str, Any]:
        """Kupon hesaplama sonucunu 5 dakika önbelleğe alarak anında yanıt verir."""
        now = time.time()
        if force_refresh or not self._coupons_cache or (now - self._last_coupons_ts > 300):
            matches = self.get_matches(force_refresh=force_refresh)
            self._coupons_cache = self.analyzer.build_smart_coupons(matches)
            self._last_coupons_ts = now
            for k, c in self._coupons_cache.items():
                if isinstance(c, dict) and "picks" in c:
                    self.tracker.record_picks(c.get("title", k), c["picks"])
        return self._coupons_cache

    def format_strategy_picks(self, strategy_key: str, top_n: int = 5) -> str:
        meta = self.analyzer.STRATEGY_METADATA.get(strategy_key)
        if not meta:
            return "❌ Geçersiz strateji kodu."

        matches = self.get_matches()
        picks = self.analyzer.scan_bulletin(matches, strategy_key, top_n=top_n, fetcher=self.fetcher)

        lines = [
            f"<b>{_safe_html(meta['title'])}</b>",
            f"<i>{_safe_html(meta['desc'])}</i>",
            f"📊 Taranan Global Maç: <b>{len(matches)}</b> | Bulunan En İyi Seçim: <b>{len(picks)}</b>",
            "━━━━━━━━━━━━━━━━━━━━━━",
        ]

        if not picks:
            lines.append("⚠️ Şu anki bültende bu stratejinin katı matematiksel filtrelerini geçen maç bulunamadı.")
            return "\n".join(lines)

        for idx, item in enumerate(picks, 1):
            a = item["analysis"]
            m = a["match"]
            s = item["signal"]
            ev_str = f"+%{s['ev_pct']:.1f} EV" if s["ev_pct"] > 0 else f"%{s['prob']:.1f} İhtimal"
            top_sc = ", ".join(f"{sc} (%{pr})" for sc, pr in a["top_scores"][:2])

            lines.append(
                f"<b>{idx}. ⚽ {_safe_html(m['home'])} - {_safe_html(m['away'])}</b>\n"
                f"   🏆 {_safe_html(m['league'])} | 🕒 {m['date']} {m['time']}\n"
                f"   🎯 <b>Tahmin:</b> {_safe_html(s['pick'])}  |  💰 <b>Oran: {s['odd']:.2f}</b>\n"
                f"   📈 <b>Model Olasılığı:</b> %{s['prob']:.1f} | 🔥 <b>Güven:</b> %{s['confidence']:.1f} ({ev_str})\n"
                f"   🧮 <b>Beklenen Gol (xG):</b> Ev {a['lam_home']} - Dep {a['lam_away']} (Top: {a['lam_total']}) | 🎲 Skor: {top_sc}\n"
                f"   💡 <b>Neden?:</b> {'; '.join(_safe_html(r) for r in s['reasons'])}"
            )
            lines.append("──────────────────────")

        return "\n".join(lines)

    def format_coupons(self) -> List[str]:
        coupons = self.get_smart_coupons()
        matches_count = len(self.get_matches())

        def _format_single_coupon(c: Dict[str, Any]) -> List[str]:
            c_lines = []
            title = _safe_html(c.get("title", ""))
            desc = _safe_html(c.get("desc", ""))
            c_lines.append(f"<b>{title}</b>")
            c_lines.append(f"<i>{desc}</i>")
            picks = c.get("picks", [])
            if not picks:
                c_lines.append("   ⚠️ Uygun maç bulunamadı.\n")
                return c_lines
            for idx, p in enumerate(picks, 1):
                c_lines.append(
                    f"  {idx}) <b>{_safe_html(p['match'])}</b> ({p['time']})\n"
                    f"     👉 <b>{_safe_html(p['pick'])}</b> | Oran: <b>{p['odd']:.2f}</b> (Model: %{p['prob']:.1f})\n"
                    f"     📌 <i>{_safe_html(p['reason'])}</i>"
                )
            c_lines.append(f"🔥 <b>Toplam Kupon Oranı: {c.get('total_odd', 0.0):.2f}</b>")
            c_lines.append("──────────────────────")
            return c_lines

        # BÖLÜM 1: 1. Garanti ve 2. İdeal Kuponlar (Telegram 4096 karakter sınırına takılmaz)
        p1_lines = [
            "🎫 <b>GÜNÜN 5 KADEMELİ KOMBİNE KUPONLARI (BÖLÜM 1/2)</b>",
            f"<i>Dixon-Coles 9x9 matrisi ve Çapraz Market (+EV) modelleriyle {matches_count} maç taranarak hazırlandı:</i>",
            "━━━━━━━━━━━━━━━━━━━━━━",
        ]
        for k in ["kupon_1_garanti", "kupon_2_ideal"]:
            if k in coupons:
                p1_lines.extend(_format_single_coupon(coupons[k]))

        # BÖLÜM 2: 3. Value, 4. Agresif ve 5. Bomba Kuponlar
        p2_lines = [
            "🎫 <b>GÜNÜN 5 KADEMELİ KOMBİNE KUPONLARI (BÖLÜM 2/2)</b>",
            "<i>(Değerli Oranlar, Yüksek Çarpanlar ve Sürpriz Vurgun Kuponları)</i>",
            "━━━━━━━━━━━━━━━━━━━━━━",
        ]
        for k in ["kupon_3_value", "kupon_4_agresif", "kupon_5_bomba"]:
            if k in coupons:
                p2_lines.extend(_format_single_coupon(coupons[k]))
        p2_lines.append(
            "💡 <i>Absürt oranlı sistem denemeleri için <b>/sistem</b> butonunu kullanabilirsiniz.</i>"
        )

        return ["\n".join(p1_lines), "\n".join(p2_lines)]

    def format_system_coupons(self) -> List[str]:
        coupons = self.get_smart_coupons()

        def _format_single_system(sc: Dict[str, Any]) -> List[str]:
            s_lines = []
            title = _safe_html(sc.get("title", ""))
            desc = _safe_html(sc.get("desc", ""))
            formula = _safe_html(sc.get("system_formula", ""))
            payout = _safe_html(sc.get("sample_payout", ""))
            s_lines.append(f"<b>{title}</b>")
            s_lines.append(f"<i>{desc}</i>")
            if formula:
                s_lines.append(f"📋 <b>{formula}</b>")
            if payout:
                s_lines.append(f"💰 <b>{payout}</b>\n")
            for idx, p in enumerate(sc.get("picks", []), 1):
                s_lines.append(
                    f"  {idx}) <b>{_safe_html(p['match'])}</b> ({p['time']})\n"
                    f"     👉 <b>{_safe_html(p['pick'])}</b> | Oran: <b>{p['odd']:.2f}</b>\n"
                    f"     📌 <i>{_safe_html(p['reason'])}</i>"
                )
            s_lines.append("━━━━━━━━━━━━━━━━━━━━━━")
            return s_lines

        # BÖLÜM 1: Sistem 8 (Kaos & Geri Dönüşler)
        p1_lines = [
            "🎰 <b>GÜNÜN 2 SİSTEM KUPONU PAKETİ (BÖLÜM 1/2)</b>",
            "<i>(İY/MS 1/2-2/1 Geri Dönüşler, +6 Gol ve İki Yarı KG Karması)</i>",
            "━━━━━━━━━━━━━━━━━━━━━━",
        ]
        if "sistem_8_kaos" in coupons:
            p1_lines.extend(_format_single_system(coupons["sistem_8_kaos"]))

        # BÖLÜM 2: Sistem 10 (Kesin Skor & Ultra Bomba) + Taktikler
        p2_lines = [
            "🌌 <b>GÜNÜN 2 SİSTEM KUPONU PAKETİ (BÖLÜM 2/2)</b>",
            "<i>(Dixon-Coles Kesin Skorlar & Ultra Yüksek Oran Denemeleri)</i>",
            "━━━━━━━━━━━━━━━━━━━━━━",
        ]
        if "sistem_10_skor" in coupons:
            p2_lines.extend(_format_single_system(coupons["sistem_10_skor"]))

        p2_lines.append(
            "⚠️ <b>Sistem Oynama Taktikleri:</b>\n"
            "• Bu kuponlar 'hepsi gelsin' diye oynanmaz; 8 maçtan 4 tanesi veya 10 maçtan 4-5 tanesi geldiğinde bile kasayı katlamak için sistem olarak işaretlenir!\n"
            "• Kasanızın en fazla %5-%10'unu bu absürt sistemlere ayırmanız önerilir."
        )

        return ["\n".join(p1_lines), "\n".join(p2_lines)]

    def format_bankroll_plan(self, amount: float = 1000.0) -> str:
        plan = self.analyzer.get_bankroll_plan(total_bankroll=amount)
        lines = [
            f"💰 <b>AKILLI KASA YÖNETİMİ & PORTFÖY DAĞITIM PLANI ({amount:,.0f} ₺)</b>",
            "<i>Profesyonel bahis sendikalarının uyguladığı risk ağırlıklı bakiye dağılımı:</i>",
            "━━━━━━━━━━━━━━━━━━━━━━",
        ]
        total_target = 0.0
        for item in plan:
            lines.append(
                f"<b>{item['name']}:</b>\n"
                f"  • Kasa Payı: <b>%{item['pct']} ({item['stake']} ₺)</b>\n"
                f"  • Tek Kupon Tutarsa Tahmini Hedef: <b>{item['target_win']:.1f} ₺</b>"
            )
            total_target += item["target_win"]
            lines.append("──────────────────────")
        lines.append(
            f"🎯 <b>Strateji Mantığı:</b>\n"
            f"1. ve 2. Garanti/İdeal kuponlar ana kasayı korur ve büyütür.\n"
            f"3., 4., 5. ve Sistem kuponları ise küçük miktarlarla büyük patlama (asimetrik getiri) hedefler."
        )
        return "\n".join(lines)

    def format_upcoming_coupons(
        self,
        hours: int = 6,
        tier_filter: Optional[int] = None,
        target_odd: Optional[float] = None,
        target_range: Optional[Tuple[float, float]] = None,
    ) -> List[str]:
        matches = self.get_matches()
        t_min, t_max = None, None
        if target_range:
            t_min, t_max = target_range
        elif target_odd:
            t_min = max(1.80, round(target_odd * 0.85, 2))
            t_max = round(target_odd * 1.25, 2)

        data = self.analyzer.build_upcoming_window_coupons(
            matches,
            hours=hours,
            tier_filter=tier_filter,
            target_min_odd=t_min,
            target_max_odd=t_max,
        )
        w_hours = data["window_hours"]
        start_t = data["start_time"]
        end_t = data["end_time"]
        cnt = data["match_count"]
        tier_badge = data.get("tier_badge", "⏱️ 6 SAATLİK MAÇLAR")
        tier_desc = data.get("tier_desc", "")

        # Kuponları karnede takip etmek üzere kaydet
        if data.get("target_coupon") and data["target_coupon"].get("picks"):
            tc = data["target_coupon"]
            self.tracker.record_coupon(tc.get("title", "Hedef Oran Kuponu"), tc["picks"], tc.get("total_odd"))
        for c in data.get("coupons", []):
            if c.get("picks"):
                self.tracker.record_coupon(c.get("title", "Yaklaşan Kupon"), c["picks"], c.get("total_odd"))

        def _format_coupon_item(c: Dict[str, Any]) -> List[str]:
            lines = [f"<b>{_safe_html(c['title'])}</b>", f"<i>{_safe_html(c['desc'])}</i>"]
            picks = c.get("picks", [])
            if not picks:
                lines.append("   ⚠️ Bu zaman aralığında bu risk profiline uygun maç bulunamadı.\n")
                return lines
            for idx, p in enumerate(picks, 1):
                m_txt = _safe_html(p["match"])
                p_txt = _safe_html(p["pick"])
                r_txt = _safe_html(p["reason"])
                lines.append(
                    f"  {idx}) <b>{m_txt}</b> ({p['time']})\n"
                    f"     👉 <b>{p_txt}</b> | Oran: <b>{p['odd']:.2f}</b> (Model: %{p['prob']:.1f})\n"
                    f"     📌 <i>{r_txt}</i>"
                )
            lines.append(f"🔥 <b>Toplam Kupon Oranı: {c['total_odd']:.2f}</b>")
            lines.append("──────────────────────")
            return lines

        # BÖLÜM 1: Hedef Oran (varsa) + 1. Garanti ve 2. İdeal Kuponlar
        p1_lines = [
            f"⏱️ <b>{tier_badge} (BÖLÜM 1/2)</b>",
            f"<i>🕒 Zaman Penceresi: <b>{start_t} - {end_t}</b> ({w_hours} Saat) | Taranan Maç: <b>{cnt}</b></i>",
            f"💡 <i>{tier_desc}</i>",
            "━━━━━━━━━━━━━━━━━━━━━━",
        ]

        if data.get("target_coupon"):
            p1_lines.append("🎯 <b>ÖZEL BELİRLENEN HEDEF ORAN KUPONUNUZ:</b>")
            p1_lines.extend(_format_coupon_item(data["target_coupon"]))

        for c in data["coupons"][:2]:
            p1_lines.extend(_format_coupon_item(c))

        # BÖLÜM 2: 3. Değer & Fırsat ve 4. Sürpriz Vurgun Kuponları
        p2_lines = [
            f"⏱️ <b>{tier_badge} (BÖLÜM 2/2)</b>",
            "<i>(Orta Riskli Değer &amp; Yüksek Oranlı Sürpriz Vurgun Kuponları)</i>",
            "━━━━━━━━━━━━━━━━━━━━━━",
        ]
        for c in data["coupons"][2:]:
            p2_lines.extend(_format_coupon_item(c))

        p2_lines.append(
            "💡 <b>Kişiselleştirme İpuçları:</b>\n"
            "• Hedef oran belirlemek için: <code>/oran 4.5</code> veya <code>/6saat oran=3-6</code> yazabilirsiniz.\n"
            "• Lig filtrelemek için: <code>/lig1</code>, <code>/lig2</code>, <code>/lig3</code> veya <code>/karisik</code> yazabilirsiniz."
        )

        return ["\n".join(p1_lines), "\n".join(p2_lines)]

    def format_scorecard(self) -> str:
        """Kupon ve tahminlerin geçmiş başarı/karne raporunu oluşturur."""
        scores_data = self.fetcher.fetch_mackolik_scores()
        fin_map = scores_data.get("finished", {})
        stats = self.tracker.update_with_results(fin_map)

        c_tot = stats["coupon_total"]
        c_won = stats["coupon_won"]
        c_lost = stats["coupon_lost"]
        c_pend = stats["coupon_pending"]
        c_win_rate = stats["coupon_win_rate"]
        c_roe = stats["coupon_roe_pct"]

        p_tot = stats["pick_total"]
        p_won = stats["pick_won"]
        p_lost = stats["pick_lost"]
        p_unv = stats["pick_unverified"]
        p_pend = stats["pick_pending"]
        p_win_rate = stats["pick_win_rate"]
        p_roi = stats["pick_roi_pct"]

        lines = [
            "📊 <b>BOT TAHMİN & KUPON BAŞARI KARNESİ (TRACK RECORD)</b>",
            "<i>Modelin önerdiği kuponlar ve tercihler resmi maç sonu skorlarıyla doğrulanır:</i>",
            "━━━━━━━━━━━━━━━━━━━━━━",
            "🎫 <b>1. KUPON BAZLI GETİRİ & ROE (Kombine Başarısı):</b>",
            f"  • <b>Değerlendirilen Tam Kupon:</b> {c_tot} Kupon",
            f"  • ✅ <b>Tutan Kuponlar:</b> {c_won} Adet",
            f"  • ❌ <b>Yatan Kuponlar:</b> {c_lost} Adet",
            f"  • 🎯 <b>Kupon Başarı Oranı:</b> %{c_win_rate:.1f}",
            f"  • 📈 <b>Net Kupon ROE (Kasa Büyümesi):</b> %{c_roe:+.1f}",
            f"  • ⏳ <b>Devam Eden / Sonuç Bekleyen:</b> {c_pend} Kupon",
            "  💡 <i>(Hesaplama: Her kupona 100 TL eşit sabit kasa bahsi baz alınmıştır.)</i>",
            "──────────────────────",
            "🎯 <b>2. TEKİL TERCİH İSABETİ & FLAT ROI (Single Bet Başarısı):</b>",
            f"  • <b>Sonuçlanan Net Tercih:</b> {p_tot} Maç",
            f"  • ✅ <b>Kazanan Seçimler:</b> {p_won} Maç",
            f"  • ❌ <b>Kaybeden Seçimler:</b> {p_lost} Maç",
            f"  • 🎯 <b>Tekil İsabet Oranı:</b> %{p_win_rate:.1f}",
            f"  • 💰 <b>Tekli Kasa Getirisi (ROI):</b> %{p_roi:+.1f}",
            f"  • ⏳ <b>Devam Eden Maçlar:</b> {p_pend} Maç",
            f"  • ⚖️ <b>Unverified (İstatistiksiz):</b> {p_unv} Maç",
            "  💡 <i>(Mackolik skorunda korner/kart istatistiği bulunmadığında yanıltıcı 'yattı' yazılmaması için istatistik dışı tutulur.)</i>",
            "──────────────────────",
        ]

        recent = stats.get("recent", [])
        if recent:
            lines.append("🏁 <b>Sonuçlanan Son Tercihler:</b>")
            for r in recent[-6:]:
                emoji = "✅" if r["status"] == "WON" else "❌"
                sc = r.get("final_score") or "-"
                lines.append(f"  {emoji} <b>{_safe_html(r['match'])}</b> ({sc}) ➔ {_safe_html(r['pick'])} @ {r['odd']:.2f}")
            lines.append("──────────────────────")
        else:
            lines.append("💡 <i>Henüz sonuçlanmış kayıtlı maç yok. Yeni kuponlar oluştukça maçlar bittikçe karne otomatik güncellenir.</i>\n──────────────────────")

        return "\n".join(lines)

    def format_dropping_odds(self) -> str:
        """Piyasada sert oran düşüşü yaşayan akıllı para maçlarını listeler."""
        # Bülteni tazeleyip oran geçmişini tara
        self.get_matches()
        drops = self.fetcher.get_dropping_odds(min_drop_pct=0.08)
        lines = [
            "📉 <b>PİYASA ORAN DÜŞÜŞ RADARI (SMART MONEY / AKILLI PARA)</b>",
            "<i>Bülten açılışından itibaren en az %8+ oran düşüşü yaşayan ve piyasanın yüklendiği maçlar:</i>",
            "━━━━━━━━━━━━━━━━━━━━━━",
        ]
        if not drops:
            lines.append("⚠️ <b>Son taramada %8 üzeri sert oran düşüşü yaşayan yeni maç tespit edilmedi.</b>\nOran hareketleri bülten yenilendikçe otomatik analiz edilir.")
            return "\n".join(lines)

        for idx, d in enumerate(drops[:8], 1):
            lines.append(
                f"<b>{idx}. ⚽ {_safe_html(d['match'])}</b> ({d['time']})\n"
                f"   🏆 {_safe_html(d['league'])}\n"
                f"   🎯 <b>Market:</b> {_safe_html(d['market_name'])}\n"
                f"   📉 <b>Oran Değişimi:</b> <s>{d['initial_odd']:.2f}</s> ➔ <b>{d['current_odd']:.2f}</b> (<b>-%{d['drop_pct']:.1f}</b> Düşüş)\n"
                f"   💡 <i>Piyasa bu tercihin riskini görerek oranı aşağı çekti (Akıllı para akışı).</i>"
            )
            lines.append("──────────────────────")

        lines.append("⚠️ <i>Taktik: Oranı sert düşen maçları tekli veya 2'li kombinelerde değerlendirmeniz önerilir.</i>")
        return "\n".join(lines)

    def format_live_radar(self) -> str:
        """Devam eden canlı maçlar arasından oyun içi (In-Play) gol fırsatlarını listeler."""
        live_data = self.fetcher.fetch_mackolik_scores()
        live_matches = live_data.get("live", [])
        pre_matches = self.get_matches()
        opps = self.analyzer.scan_live_opportunities(live_matches, pre_matches)

        lines = [
            "🔴 <b>CANLI MAÇ OYUN İÇİ (IN-PLAY) FIRSAT RADARI</b>",
            f"<i>Şu an anlık oynanan <b>{len(live_matches)}</b> canlı maç maç öncesi xG & tempo modelleriyle tarandı:</i>",
            "━━━━━━━━━━━━━━━━━━━━━━",
        ]

        if not opps:
            lines.append(
                "⚠️ <b>Şu an canlıda model kriterlerine uyan aktif fırsat yok.</b>\n"
                f"• Anlık izlenen canlı maç sayısı: <b>{len(live_matches)}</b>\n"
                "• Radar; maç öncesi gol beklentisi yüksek olup 40. dakikadan sonra kilitlenen maçları ve geriye düşen favorileri otomatik yakalar.\n"
                "• Yoğun maç saatlerinde tekrar deneyin."
            )
            return "\n".join(lines)

        for idx, o in enumerate(opps[:6], 1):
            lines.append(
                f"<b>{idx}. 🔴 {_safe_html(o['match'])}</b> ({o['status']})\n"
                f"   🏆 {_safe_html(o['league'])}\n"
                f"   🎯 <b>Canlı Sinyal:</b> <b>{_safe_html(o['opportunity_type'])}</b>\n"
                f"   📈 <b>Model Güveni:</b> %{o['confidence']:.1f} (xG: {o['expected_goals']:.2f})\n"
                f"   💡 <i>{_safe_html(o['reason'])}</i>"
            )
            lines.append("──────────────────────")

        lines.append("⚡ <i>Canlı bahiste oranlar dakikalar içinde değiştiğinden hızlı aksiyon önemlidir.</i>")
        return "\n".join(lines)

    def format_wizard_coupon(self, match_count: int = 3, min_odd: float = 2.0, max_odd: float = 6.0, risk_level: str = "ideal") -> str:
        """Kullanıcının tercihine göre kişiselleştirilmiş sihirbaz kuponu oluşturur."""
        matches = self.get_matches()
        c = self.analyzer.build_custom_wizard_coupon(
            matches=matches,
            match_count=match_count,
            min_total_odd=min_odd,
            max_total_odd=max_odd,
            risk_level=risk_level,
        )

        lines = [
            f"🧙‍♂️ <b>{_safe_html(c['title'])}</b>",
            f"<i>Parametreler: {match_count} Maç | Hedef Oran: {min_odd:.2f} - {max_odd:.2f} | Risk: {risk_level.capitalize()}</i>",
            "━━━━━━━━━━━━━━━━━━━━━━",
        ]
        picks = c.get("picks", [])
        if not picks:
            lines.append("⚠️ Belirtilen kriterlere uygun maç kombinasyonu bulunamadı.")
            return "\n".join(lines)

        for idx, p in enumerate(picks, 1):
            lines.append(
                f"  {idx}) <b>{_safe_html(p['match'])}</b> ({p['time']})\n"
                f"     👉 <b>{_safe_html(p['pick'])}</b> | Oran: <b>{p['odd']:.2f}</b> (Model: %{p['prob']:.1f})\n"
                f"     📌 <i>{_safe_html(p['reason'])}</i>"
            )
        lines.append(f"🔥 <b>Toplam Kupon Oranı: {c['total_odd']:.2f}</b>")
        lines.append("──────────────────────")
        lines.append(
            "💡 <i>Farklı kriterlerle denemek için: <code>/sihirbaz mac=4 oran=3-8 risk=garanti</code> yazabilirsiniz.</i>"
        )
        return "\n".join(lines)

    def format_trap_radar(self) -> str:
        """Piyasanın favoriye yüklendiği ama modelin yüksek patlama tehlikesi gördüğü tuzak maçları listeler."""
        matches = self.get_matches()
        traps = self.analyzer.scan_trap_matches(matches, top_n=5)

        lines = [
            "⚠️ <b>TERS KÖŞE & TUZAK MAÇ RADARI (ANTI-TRAP / RLM)</b>",
            "<i>Halkın ve kamuoyunun favoriye aşırı yüklendiği ancak modelin patlama riski gördüğü maçlar:</i>",
            "━━━━━━━━━━━━━━━━━━━━━━",
        ]
        if not traps:
            lines.append("🛡️ <b>Şu anki bültende keskin bir 'Favori Tuzağı' sinyali tespit edilmedi.</b>")
            lines.append("Bültendeki favoriler model beklentileriyle makul uyum içinde görünüyor.")
            return "\n".join(lines)

        for idx, t in enumerate(traps, 1):
            m = t["match"]
            lines.append(
                f"{idx}) 🚨 <b>{_safe_html(m['home'])} - {_safe_html(m['away'])}</b> ({m.get('time', '')})\n"
                f"   👑 <b>Ağır Favori:</b> {_safe_html(t['favorite'])} (Oran: <b>{t['favorite_odd']:.2f}</b>)\n"
                f"   💣 <b>Tuzak Skoru:</b> {t['trap_score']}/100 ({t['danger_level']})\n"
                f"   📊 <b>Piyasa Beklentisi:</b> %{t['implied_market_prob']:.1f} ➔ <b>Model Güveni:</b> %{t['model_prob']:.1f}\n"
                f"   💡 <b>Risk Sebebi:</b> <i>{_safe_html(t['reasons'][0])}</i>\n"
                f"   🛡️ <b>Anti-Tuzak Önerisi:</b> 👉 <b>{_safe_html(t['anti_pick'])}</b>"
            )
            lines.append("──────────────────────")

        lines.append("💡 <i>Kupon yaparken oranına aldanıp doğrudan favoriye girmemek kasanızı korur!</i>")
        return "\n".join(lines)

    def format_tension_radar(self) -> str:
        """Derbi, sert lig mücadeleleri ve yüksek kart/korner tansiyonlu maçları listeler."""
        matches = self.get_matches()
        tensions = self.analyzer.scan_tension_matches(matches, top_n=5)

        lines = [
            "🟨 <b>HAKEM & MAÇ TANSİYON RADARI (KART & KORNER)</b>",
            "<i>Yüksek rekabetli, sert ikili mücadele ve kart baskısı tavan maçlar:</i>",
            "━━━━━━━━━━━━━━━━━━━━━━",
        ]
        if not tensions:
            lines.append("⚖️ Şu an bültende belirgin yüksek tansiyon veya derbi gerginliği sinyali yok.")
            return "\n".join(lines)

        for idx, ten in enumerate(tensions, 1):
            m = ten["match"]
            lines.append(
                f"{idx}) 🔥 <b>{_safe_html(m['home'])} - {_safe_html(m['away'])}</b> ({m.get('time', '')})\n"
                f"   🏆 <b>Lig:</b> {_safe_html(ten['league'])}\n"
                f"   ⚔️ <b>Tansiyon Skoru:</b> {ten['tension_score']}/100\n"
                f"   🟨 <b>Beklenen Kart:</b> <b>{ten['expected_cards']} Kart</b> (Model: %{ten['card_prob']:.1f})\n"
                f"   📐 <b>Beklenen Korner:</b> <b>{ten['expected_corners']} Korner</b>\n"
                f"   🔴 <b>Kırmızı Kart İhtimali:</b> %{ten['red_card_risk']:.1f} (Yüksek Tehdit!)\n"
                f"   👉 <b>Tavsiye Tercih:</b> <b>{ten['card_pick']}</b> veya <b>{ten['corner_pick']}</b>"
            )
            lines.append("──────────────────────")

        lines.append("💡 <i>Kart ve korner bahisleri hakem ve lig sertliği modelleriyle en yüksek başarı getiren alanlardandır.</i>")
        return "\n".join(lines)

    def format_monte_carlo(self, query: str = "") -> str:
        """Dixon-Coles xG olasılıklarıyla maçı 10.000 kez bilgisayarda simüle eder."""
        matches = self.get_matches()
        if not matches:
            return "⚠️ Bültende simülasyon yapılacak maç bulunamadı."

        selected_match = None
        if query.strip():
            q = query.strip().lower()
            found = [m for m in matches if q in m["home"].lower() or q in m["away"].lower() or q == m.get("code")]
            if found:
                selected_match = found[0]

        if not selected_match:
            selected_match = matches[0]

        sim = self.analyzer.run_monte_carlo_simulation(selected_match, n_sims=10000)

        lines = [
            "🎲 <b>MONTE CARLO 10.000 MAÇ SİMÜLASYONU</b>",
            f"⚽ <b>{_safe_html(sim['home'])} vs {_safe_html(sim['away'])}</b>",
            f"🏆 {_safe_html(sim['league'])} | 🕒 {sim['date']} {sim['time']}",
            "<i>Bu maç Poisson & Dixon-Coles xG dağılımıyla tam 10.000 kez sanal olarak oynatıldı:</i>",
            "━━━━━━━━━━━━━━━━━━━━━━",
            "📊 <b>10.000 Simülasyon Galibiyet Dağılımı:</b>",
            f"  • 🏠 Ev Sahibi Galibiyeti: <b>%{sim['home_win_pct']:.1f}</b> ({int(sim['home_win_pct'] * 100)} maç)",
            f"  • 🤝 Beraberlik: <b>%{sim['draw_pct']:.1f}</b> ({int(sim['draw_pct'] * 100)} maç)",
            f"  • ✈️ Deplasman Galibiyeti: <b>%{sim['away_win_pct']:.1f}</b> ({int(sim['away_win_pct'] * 100)} maç)",
            "──────────────────────",
            "⚽ <b>Gol Limitleri & Karşılıklı Gol:</b>",
            f"  • 1.5 Gol Üst: <b>%{sim['over_15_pct']:.1f}</b>",
            f"  • 2.5 Gol Üst: <b>%{sim['over_25_pct']:.1f}</b>",
            f"  • 3.5 Gol Üst: <b>%{sim['over_35_pct']:.1f}</b>",
            f"  • KG Var (İki Takım da Attı): <b>%{sim['btts_pct']:.1f}</b>",
            f"  • Simüle Edilen Maç Başı Ortalama Gol: <b>{sim['avg_goals']:.2f} Gol</b>",
            "──────────────────────",
            "🎯 <b>10.000 Maçta En Sık Çıkan 5 Kesin Skor:</b>",
        ]
        for idx, sc in enumerate(sim["top_scores"], 1):
            lines.append(f"  {idx}) <b>{sc['score']}</b> ➔ {sc['count']} kez (<b>%{sc['pct']:.1f}</b> frekans)")

        lines.append("──────────────────────")
        lines.append(
            "💡 <i>Farklı bir maçı simüle etmek için: <code>/simulasyon TakımAdı</code> yazabilirsiniz (Örn: <code>/simulasyon Chelsea</code>).</i>"
        )
        return "\n".join(lines)

    def format_vip_broadcast(self) -> List[str]:
        """VIP Kanallar ve Gruplar için şık, profesyonel günlük bülten ve kupon paketi hazırlar."""
        coupons = self.format_coupons()
        trap_radar = self.format_trap_radar()

        header = (
            "💎👑 <b>GLOBAL QUANT VIP GÜNLÜK BAHİS BÜLTENİ</b> 👑💎\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "📅 <b>Bugünün Dixon-Coles 9x9 & Sharp Value Analiz Raporu</b>\n"
            "Algoritmamız global bültendeki tüm maçları tarayarak en yüksek kazanma beklentisine sahip "
            "banko, değerli (+EV) ve sürpriz kombinasyonları hazırladı.\n"
            "━━━━━━━━━━━━━━━━━━━━━━"
        )
        part1 = f"{header}\n\n{coupons[0] if isinstance(coupons, list) else coupons}"
        part2 = f"⚠️ <b>GÜNÜN TUZAK VE TERS KÖŞE UYARILARI:</b>\n{trap_radar}"
        return [part1, part2]

    def format_match_xray(self, query: str) -> str:
        query_clean = query.strip().lower()
        if not query_clean:
            return "⚠️ Lütfen bir takım adı yazın. Örnek: <code>/analiz Chelsea</code> veya <code>/analiz Galatasaray</code>"

        matches = self.get_matches()
        found = [
            m for m in matches
            if query_clean in m["home"].lower() or query_clean in m["away"].lower() or query_clean == m["code"]
        ]
        if not found:
            return f"❌ <b>'{query}'</b> aramasına uygun bültende aktif maç bulunamadı."

        m = found[0]
        espn_det = None
        if m.get("espn") and m["espn"].get("event_id"):
            espn_det = self.fetcher.fetch_espn_match_details(m["espn"]["event_id"])
        a = self.analyzer.analyze_match(m, espn_details=espn_det)
        p = a["probabilities"]
        odds = m["odds"]

        lines = [
            f"🔬 <b>MAÇ X-RAY DERİN ANALİZ RAPORU</b>",
            f"⚽ <b>{m['home']} vs {m['away']}</b>",
            f"🏆 {m['league']} | 🕒 {m['date']} {m['time']} | Kod: #{m['code']}",
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"🧮 <b>Bivariate Poisson & xG Beklentisi:</b>",
            f"  • Ev Sahibi (λ_ev): <b>{a['lam_home']} gol</b>",
            f"  • Deplasman (λ_dep): <b>{a['lam_away']} gol</b>",
            f"  • Toplam Gol (λ_top): <b>{a['lam_total']} gol</b> (İlk Yarı λ_1Y: {a['lam_1h']})",
            f"  • Kaos / Varyans Endeksi: <b>{a['chaos_index']}/100</b>",
            f"  • En Olası 3 Kesin Skor: <b>" + " | ".join(f"{sc} (%{pr})" for sc, pr in a["top_scores"]) + "</b>",
            "──────────────────────",
            f"📊 <b>Model Olasılıkları vs Bahis Oranları:</b>",
            f"  • MS 1 / X / 2: <b>%{p['ms_1']}</b> ({odds.get('ms_1', '-')}) | <b>%{p['ms_x']}</b> ({odds.get('ms_x', '-')}) | <b>%{p['ms_2']}</b> ({odds.get('ms_2', '-')})",
            f"  • KG Var: <b>%{p['kg_var']}</b> (Oran: {odds.get('kg_var', '-')})",
            f"  • 2.5 Üst: <b>%{p['ust_25']}</b> (Oran: {odds.get('ust_25', '-')}) | 3.5 Üst: <b>%{p['ust_35']}</b> (Oran: {odds.get('ust_35', '-')})",
            f"  • İlk Yarı 1.5 Üst: <b>%{p['iy_ust_15']}</b> (Oran: {odds.get('iy_ust_15', '-')})",
            f"  • 1Y KG Var: <b>%{p['iy_kg_var']}</b> ({odds.get('iy_kg_var', '-')}) | 2Y KG Var: <b>%{p['ikinci_yari_kg_var']}</b> ({odds.get('ikinci_yari_kg_var', '-')})",
            f"  • İki Yarıda da KG Var (Evet/Evet): <b>%{p['iki_yari_kg']}</b> (Oran: {odds.get('iki_yari_kg_var', '-')})",
            f"  • +6 Gol (6+ Gol): <b>%{p['arti_6_gol']}</b> (Oran: {odds.get('arti_6_gol', '-')})",
        ]

        if a["form_notes"]:
            lines.append("──────────────────────")
            lines.append("📈 <b>Son 5 Maç Form & H2H Verisi:</b>")
            for fn in a["form_notes"]:
                lines.append(f"  • {fn}")

        lines.append("──────────────────────")
        lines.append("🎯 <b>BU MAÇ İÇİN TETİKLENEN STRATEJİ SİNYALLERİ:</b>")
        if not a["signals"]:
            lines.append("  • Bu maç dengeli/standart sınırlar içinde; keskin bir taktik filtresini tetiklemedi.")
        else:
            sorted_sigs = sorted(a["signals"].items(), key=lambda x: x[1]["confidence"], reverse=True)
            for skey, sig in sorted_sigs:
                lines.append(
                    f"  ✅ <b>{sig['pick']}</b> (Oran: <b>{sig['odd']:.2f}</b> | Güven: %{sig['confidence']:.1f})\n"
                    f"     └ <i>{sig['reasons'][0]}</i>"
                )

        return "\n".join(lines)

    @staticmethod
    def format_methodology_explanation() -> str:
        return (
            "📚 <b>BOTUN KULLANDIĞI GLOBAL BAHİS STRATEJİLERİ VE MATEMATİKSEL MODELLER</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "Globalde profesyonel bahis sendikalarının (Smart Money / Quant Bettors) kullandığı 6 temel model bu botta birleştirilmiştir:\n\n"
            "1️⃣ <b>Multiplicative / Shin Vig-Removal (Kar Marjı Arındırma):</b>\n"
            "Bahis şirketleri oranlara %5 ila %12 kar marjı (overround) ekler. Bot önce tüm marketlerdeki bu marjı silerek piyasanın 'gerçek (adil) olasılıklarını' çıkarır.\n\n"
            "2️⃣ <b>Inverse Multi-Market Poisson (Çoklu Marketten xG / Gol Beklentisi Çözümleme):</b>\n"
            "Sadece tek bir orana bakmak yerine; 1.5 Üst, 2.5 Üst, 3.5 Üst, Ev 1.5 Üst, Dep 1.5 Üst, MS ve KG Var oranlarını aynı anda ters-Poisson (Bisection) denklemine sokarak her iki takımın gizli gol beklentisini (<b>λ_ev</b> ve <b>λ_dep</b>) hesaplar ve ESPN/Mackolik son 5 maç form verisiyle harmanlar.\n\n"
            "3️⃣ <b>Dixon-Coles (1997) Düşük Skor Korelasyonlu 9x9 Skor Matrisi:</b>\n"
            "Standart Poisson modeli 0-0, 1-1, 1-0 ve 0-1 skorlarını hatalı tahmin eder. Dixon-Coles modeli <b>ρ (rho)</b> korelasyon katsayısıyla 0-0'dan 8-8'e kadar 81 olası skorun tamamının olasılığını ayrı ayrı hesaplar.\n\n"
            "4️⃣ <b>Half-Split Tempo Modeli (İki Yarı KG Var & İY 1.5 Üst Taktiği):</b>\n"
            "Global futbol verisinde gollerin %44'ü 1. yarıda, %56'sı 2. yarıda atılır. Bot, <i>'İki Yarıda da KG Var (Evet/Evet)'</i> taktiği için tek taraflı (5-0 bitecek) maçları eler; yalnızca <b>Simetrik Yüksek Tempo (λ_ev ≥ 1.25 ve λ_dep ≥ 1.15)</b> olan ve İlk Yarı KG/1.5 Üst marketlerinin onayladığı maçları seçer.\n\n"
            "5️⃣ <b>Fat-Tail Volatility / Kaos Endeksi (+6 Gol Taktiği):</b>\n"
            "<i>+6 Gol</i> normalde %2-%3 ihtimaldir. Ancak toplam gol beklentisi λ ≥ 3.25 olan ve hem ağır favori baskısı hem de deplasman kontra golü barındıran maçlarda Poisson sağ-kuyruk (Fat-Tail) olasılığı %10-%16 bandına fırlar (normalin 4-5 katı!).\n\n"
            "6️⃣ <b>Cross-Market Value Bet (+EV) & Quarter-Kelly Kriteri:</b>\n"
            "Çapraz marketlerden çıkan gerçek olasılık, bahis şirketinin verdiği orandan yüksekse (<b>Olasılık × Oran > 1.045</b>) orada <b>Oran Sapması (Value Bet)</b> vardır. Bot ayrıca kasanızın yüzde kaçıyla girmeniz gerektiğini <i>Quarter-Kelly</i> formülüyle söyler."
        )


class TelegramBettingBot:
    """Resmi Telegram Bot HTTP API (Long-Polling + Inline Keyboard) istemcisi."""

    STRATEGY_BUTTONS = [
        [
            {"text": "🎯 KG Var", "callback_data": "strat:kg_var"},
            {"text": "🏆 MS Banko/Value", "callback_data": "strat:ms_banko"},
        ],
        [
            {"text": "🔥 2.5 Üst Taktiği", "callback_data": "strat:ust_25"},
            {"text": "🛡️ 2.5 Alt (Duvar)", "callback_data": "strat:alt_taktigi"},
        ],
        [
            {"text": "🚀 3.5 Üst Fırtınası", "callback_data": "strat:ust_35"},
            {"text": "💣 +6 Gol (Kaos)", "callback_data": "strat:arti_6_gol"},
        ],
        [
            {"text": "⚡ İki Yarı KG (Evet/Evet)", "callback_data": "strat:iki_yari_kg"},
            {"text": "🏠 Ev/Dep İki Yarı Gol", "callback_data": "strat:takim_iki_yari_gol"},
        ],
        [
            {"text": "🔄 İY/MS Geri Dönüş (1/2-2/1)", "callback_data": "strat:iyms_surpriz"},
            {"text": "🎯 Kesin Skor Sniper", "callback_data": "strat:kesin_skor"},
        ],
        [
            {"text": "🟨 Kart & Korner Radarı", "callback_data": "strat:kart_korner"},
            {"text": "⚖️ 2-3 / 4-5 Gol Aralığı", "callback_data": "strat:tg_aralik"},
        ],
        [
            {"text": "⏱️ İlk Yarı 1.5 Üst", "callback_data": "strat:iy_ust"},
            {"text": "💎 Kombo (KG/MS & Üst)", "callback_data": "strat:kombo"},
        ],
        [
            {"text": "🧠 Sharp Value Bet (+EV)", "callback_data": "strat:value_bet"},
            {"text": "💰 Akıllı Kasa Dağıtımı", "callback_data": "action:kasa"},
        ],
        [
            {"text": "🔴 Canlı Fırsat Radarı (In-Play)", "callback_data": "action:live_radar"},
            {"text": "📉 Oran Düşüşü (Smart Money)", "callback_data": "action:dropping"},
        ],
        [
            {"text": "🧙‍♂️ Akıllı Kupon Sihirbazı", "callback_data": "action:wizard"},
            {"text": "📊 Başarı Karnesi & ROI", "callback_data": "action:scorecard"},
        ],
        [
            {"text": "⚠️ Tuzak Maç Radarı", "callback_data": "action:trap"},
            {"text": "🟨 Tansiyon & Kart Radarı", "callback_data": "action:tension"},
        ],
        [
            {"text": "🎲 Monte Carlo 10K Simülasyon", "callback_data": "action:monte_carlo"},
            {"text": "📢 Günlük VIP Bülten", "callback_data": "action:vip_broadcast"},
        ],
        [
            {"text": "⏱️ 6 Saat: Lig 1 (Ana Ligler)", "callback_data": "action:upcoming_tier1"},
            {"text": "⏱️ 6 Saat: Lig 2 (2. Ligler)", "callback_data": "action:upcoming_tier2"},
        ],
        [
            {"text": "⏱️ 6 Saat: Lig 3 (Alt Ligler)", "callback_data": "action:upcoming_tier3"},
            {"text": "⏱️ 6 Saat: Karışık (Tümü)", "callback_data": "action:upcoming_all"},
        ],
        [
            {"text": "🎯 6 Saat: Hedef Oran 3-5", "callback_data": "action:uptarget:3_5"},
            {"text": "🎯 6 Saat: Hedef Oran 5-10", "callback_data": "action:uptarget:5_10"},
        ],
        [
            {"text": "🎫 5 Kademeli Kuponlar (Garanti ➔ Bomba)", "callback_data": "action:coupons"},
        ],
        [
            {"text": "🎰 2 Absürt Oran Sistem Kuponu (8 & 10 Maç)", "callback_data": "action:system"},
        ],
        [
            {"text": "📚 Global Modeller", "callback_data": "action:models"},
            {"text": "🔄 Bülteni Yenile", "callback_data": "action:refresh"},
        ],
    ]

    def __init__(self, token: str, formatter: BettingBotFormatter):
        self.token = token
        self.formatter = formatter
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.offset = 0
        self.subscribers: Set[int] = self._load_subscribers()
        self.sent_alerts: Set[str] = set()
        self.vip_channel_id: Optional[str] = os.environ.get("VIP_CHANNEL_ID") or self._load_vip_channel()

    def _load_vip_channel(self) -> Optional[str]:
        if os.path.exists(VIP_CHANNEL_PATH):
            try:
                with open(VIP_CHANNEL_PATH, "r", encoding="utf-8") as f:
                    v = f.read().strip()
                    return v if v else None
            except Exception:
                pass
        return None

    def _save_vip_channel(self, channel_id: str) -> None:
        try:
            with open(VIP_CHANNEL_PATH, "w", encoding="utf-8") as f:
                f.write(channel_id.strip())
        except Exception:
            pass

    def _load_subscribers(self) -> Set[int]:
        if os.path.exists(SUBSCRIBERS_PATH):
            try:
                with open(SUBSCRIBERS_PATH, "r", encoding="utf-8") as f:
                    return set(json.load(f))
            except Exception:
                pass
        return set()

    def _save_subscribers(self) -> None:
        try:
            with open(SUBSCRIBERS_PATH, "w", encoding="utf-8") as f:
                json.dump(list(self.subscribers), f)
        except Exception:
            pass

    def _api_call(self, method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.base_url}/{method}"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=35) as resp:
            return json.loads(resp.read().decode("utf-8"))

    @staticmethod
    def _smart_split_text(text: str, max_len: int = 3600) -> List[str]:
        """Metni HTML etiketlerini veya maç bloklarını kırmadan satır sonlarından böler."""
        if len(text) <= max_len:
            return [text]
        chunks: List[str] = []
        cur_lines: List[str] = []
        cur_len = 0
        for line in text.split("\n"):
            line_len = len(line) + 1
            if cur_len + line_len > max_len and cur_lines:
                chunks.append("\n".join(cur_lines))
                cur_lines = [line]
                cur_len = line_len
            else:
                cur_lines.append(line)
                cur_len += line_len
        if cur_lines:
            chunks.append("\n".join(cur_lines))
        return chunks

    def send_message(self, chat_id: int, content: Any, show_menu: bool = True) -> None:
        """
        Mesajı veya mesaj listesini güvenle Telegram'a gönderir.
        HTML ayrıştırma hatası olursa anında düz metin (plain text) yedeğiyle yeniden dener.
        """
        if isinstance(content, list):
            items = content
        else:
            items = [str(content)]

        for idx, raw_text in enumerate(items):
            is_last = (idx == len(items) - 1)
            chunks = self._smart_split_text(raw_text, max_len=3600)

            for c_idx, chunk in enumerate(chunks):
                attach_menu = show_menu and is_last and (c_idx == len(chunks) - 1)
                payload: Dict[str, Any] = {
                    "chat_id": chat_id,
                    "text": chunk,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                }
                if attach_menu:
                    payload["reply_markup"] = {"inline_keyboard": self.STRATEGY_BUTTONS}

                try:
                    self._api_call("sendMessage", payload)
                except Exception as e:
                    print(f"[Telegram Send Warning] {e} -> Düz metin olarak yeniden deneniyor...")
                    payload["parse_mode"] = None
                    payload["text"] = re.sub(r"<[^>]+>", "", chunk)
                    try:
                        self._api_call("sendMessage", payload)
                    except Exception as e2:
                        print(f"[Telegram Plaintext Send Error] {e2}")

    def handle_command(self, chat_id: int, text: str) -> None:
        parts = text.strip().split(maxsplit=1)
        cmd = parts[0].lower().split("@")[0]
        arg = parts[1] if len(parts) > 1 else ""

        cmd_map = {
            "/kg": "kg_var",
            "/ms": "ms_banko",
            "/ust25": "ust_25",
            "/alt": "alt_taktigi",
            "/ust35": "ust_35",
            "/arti6": "arti_6_gol",
            "/ikiyarikg": "iki_yari_kg",
            "/takimgol": "takim_iki_yari_gol",
            "/iyms": "iyms_surpriz",
            "/skor": "kesin_skor",
            "/kart": "kart_korner",
            "/tgaralik": "tg_aralik",
            "/iyust": "iy_ust",
            "/kombo": "kombo",
            "/korner": "korner",
            "/value": "value_bet",
        }

        if cmd in ("/start", "/menu", "/help"):
            welcome = (
                "🤖 <b>GLOBAL QUANT BAHİS & İDDAA TAHMİN BOTUNA HOŞ GELDİNİZ!</b>\n\n"
                "Bu bot; global bültendeki 1.300+ maçı <b>Dixon-Coles 9x9 Skor Matrisi</b>, "
                "<b>Inverse Multi-Market Poisson (xG)</b>, <b>Half-Split Tempo</b> ve "
                "<b>Sharp Value (+EV)</b> modelleriyle tarar.\n\n"
                "👇 <b>Aşağıdaki butonlardan istediğiniz taktiği seçebilir veya komutları kullanabilirsiniz:</b>\n"
                "• <code>/canli</code> — 🔴 Canlı Fırsat Radarı (In-Play Gol & Baskı Takibi)\n"
                "• <code>/dusen</code> — 📉 Oran Düşüşü (Smart Money & Para Akışı)\n"
                "• <code>/sihirbaz</code> — 🧙‍♂️ Akıllı Kupon Sihirbazı (Örn: <code>/sihirbaz mac=4 oran=3-8 risk=garanti</code>)\n"
                "• <code>/karne</code> — 📊 Model Başarı Karnesi & ROI Takibi\n"
                "• <code>/tuzak</code> — ⚠️ Ters Köşe & Tuzak Maç Radarı (Anti-Trap)\n"
                "• <code>/tansiyon</code> — 🟨 Hakem & Maç Tansiyon Radarı (Kart & Korner)\n"
                "• <code>/simulasyon &lt;takım&gt;</code> — 🎲 Monte Carlo 10.000 Simülasyon\n"
                "• <code>/yayinla</code> — 📢 Günlük VIP Bülteni Kanala/Sohbete Yayınla\n"
                "• <code>/lig1</code> — 👑 6 Saatlik Lig 1 (Ana Ligler & Nesine 1)\n"
                "• <code>/lig2</code> — 🥈 6 Saatlik Lig 2 (2. Ligler & Kaliteli Ligler)\n"
                "• <code>/lig3</code> — 🥉 6 Saatlik Lig 3 (Alt Ligler & Diğerleri)\n"
                "• <code>/karisik</code> — 🎲 6 Saatlik Karışık (Tüm Ligler)\n"
                "• <code>/6saat</code> — ⏱️ Önümüzdeki 6 Saatin Maçları (Garantiden Riskliye)\n"
                "• <code>/kupon</code> — 🎫 5 Kademeli Günlük Kupon (Garanti ➔ Bomba)\n"
                "• <code>/sistem</code> — 🎰 2 Absürt Oran Sistem Kuponu (8 & 10 Maç)\n"
                "• <code>/kasa &lt;tutar&gt;</code> — 💰 Akıllı Kasa Dağıtım Planı (Örn: <code>/kasa 1000</code>)\n"
                "• <code>/kg</code> — 🎯 KG Var Taktiği\n"
                "• <code>/ms</code> — 🏆 MS Favori & Value Taktiği\n"
                "• <code>/ust25</code> / <code>/alt</code> — 🔥 2.5 Üst / 🛡️ 2.5 Alt (Duvar)\n"
                "• <code>/ust35</code> / <code>/arti6</code> — 🚀 3.5 Üst / 💣 +6 Gol (Kaos)\n"
                "• <code>/ikiyarikg</code> — ⚡ İki Yarıda da KG Var (Evet/Evet)\n"
                "• <code>/takimgol</code> — 🏠 Ev/Dep Her İki Yarıda Gol Atar\n"
                "• <code>/iyms</code> — 🔄 İY/MS Geri Dönüşler (1/2-2/1) & X/1-X/2\n"
                "• <code>/skor</code> — 🎯 Dixon-Coles Kesin Skor Sniper\n"
                "• <code>/kart</code> — 🟨 Kart & Korner Baskı Radarı\n"
                "• <code>/tgaralik</code> — ⚖️ 2-3 Gol & 4-5 Gol Aralığı\n"
                "• <code>/value</code> — 🧠 Oran Sapması (+EV) Value Bet\n"
                "• <code>/analiz TakımAdı</code> — 🔬 Maç X-Ray Analizi (Örn: <code>/analiz Chelsea</code>)\n"
                "• <code>/kanal &lt;ID&gt;</code> — 📡 VIP Yayın Kanalı Belirle (Örn: <code>/kanal @Kanalim</code>)\n"
                "• <code>/oto_ac</code> / <code>/oto_kapat</code> — 🔔 Otomatik Fırsat Bildirimi"
            )
            self.send_message(chat_id, welcome, show_menu=True)

        elif cmd in cmd_map:
            reply = self.formatter.format_strategy_picks(cmd_map[cmd])
            self.send_message(chat_id, reply, show_menu=True)

        elif cmd in ("/canli", "/live"):
            self.send_message(chat_id, self.formatter.format_live_radar(), show_menu=True)

        elif cmd in ("/dusen", "/dropping"):
            self.send_message(chat_id, self.formatter.format_dropping_odds(), show_menu=True)

        elif cmd in ("/karne", "/performans", "/rapor"):
            self.send_message(chat_id, self.formatter.format_scorecard(), show_menu=True)

        elif cmd in ("/tuzak", "/trap"):
            self.send_message(chat_id, self.formatter.format_trap_radar(), show_menu=True)

        elif cmd in ("/tansiyon", "/hakem", "/kartlar"):
            self.send_message(chat_id, self.formatter.format_tension_radar(), show_menu=True)

        elif cmd in ("/simulasyon", "/montecarlo", "/mc"):
            self.send_message(chat_id, self.formatter.format_monte_carlo(arg), show_menu=True)

        elif cmd in ("/yayinla", "/vip"):
            broadcast_content = self.formatter.format_vip_broadcast()
            self.send_message(chat_id, broadcast_content, show_menu=True)
            if self.vip_channel_id:
                try:
                    target_ch = int(self.vip_channel_id) if (self.vip_channel_id.startswith("-") or self.vip_channel_id.isdigit()) else self.vip_channel_id
                    self.send_message(target_ch, broadcast_content, show_menu=False)
                    self.send_message(chat_id, f"✅ VIP Günlük Bülten <b>{self.vip_channel_id}</b> kanalına başarıyla yayınlandı!", show_menu=False)
                except Exception as e:
                    self.send_message(chat_id, f"⚠️ VIP Kanala yayınlanırken hata oluştu: <code>{e}</code>", show_menu=False)
            else:
                self.send_message(chat_id, "💡 <i>Kalıcı bir VIP kanala otomatik yayınlamak için: <code>/kanal @KanalKullaniciAdi</code> komutunu kullanabilirsiniz.</i>", show_menu=False)

        elif cmd == "/kanal":
            if not arg.strip():
                cur = self.vip_channel_id or "Ayarlanmadı"
                self.send_message(chat_id, f"📡 <b>Mevcut VIP Yayın Kanalı:</b> <code>{cur}</code>\n\nDeğiştirmek için: <code>/kanal @KanalKullaniciAdi</code> veya <code>/kanal -100xxxxxxxx</code> yazabilirsiniz.", show_menu=True)
            else:
                ch = arg.strip()
                self.vip_channel_id = ch
                self._save_vip_channel(ch)
                self.send_message(chat_id, f"✅ <b>VIP Kanal Kaydedildi:</b> <code>{ch}</code>\nArtık <code>/yayinla</code> komutu bülteni doğrudan bu kanala da ulaştıracak.", show_menu=True)

        elif cmd in ("/sihirbaz", "/wizard"):
            match_count = 3
            min_odd = 2.0
            max_odd = 6.0
            risk_level = "ideal"
            if arg:
                for token in arg.split():
                    t_lower = token.lower()
                    if t_lower.startswith("mac="):
                        val = t_lower.replace("mac=", "")
                        if val.isdigit():
                            match_count = int(val)
                    elif t_lower.startswith("oran="):
                        val = t_lower.replace("oran=", "")
                        if "-" in val:
                            p_o = val.split("-")
                            try:
                                min_odd = float(p_o[0])
                                max_odd = float(p_o[1])
                            except ValueError:
                                pass
                        else:
                            try:
                                max_odd = float(val)
                                min_odd = max(1.5, max_odd * 0.7)
                            except ValueError:
                                pass
                    elif t_lower.startswith("risk="):
                        val = t_lower.replace("risk=", "")
                        if val in ("garanti", "ideal", "surpriz", "bomba"):
                            risk_level = val
                    elif token.isdigit():
                        match_count = int(token)
            self.send_message(
                chat_id,
                self.formatter.format_wizard_coupon(
                    match_count=match_count,
                    min_odd=min_odd,
                    max_odd=max_odd,
                    risk_level=risk_level,
                ),
                show_menu=True,
            )

        elif cmd in ("/6saat", "/4saat", "/yaklasan", "/lig1", "/lig2", "/lig3", "/karisik", "/hepsi", "/oran", "/hedef"):
            target_odd, target_range = None, None
            tier_filter = 1  # Varsayılan: Lig 1 (Ana ve Elit Ligler)
            arg_lower = arg.strip().lower()

            if cmd in ("/lig1", "/tier1"):
                tier_filter = 1
            elif cmd in ("/lig2", "/tier2"):
                tier_filter = 2
            elif cmd in ("/lig3", "/tier3"):
                tier_filter = 3
            elif cmd in ("/karisik", "/hepsi"):
                tier_filter = None
            elif "lig2" in arg_lower or "lig 2" in arg_lower or arg_lower == "2":
                tier_filter = 2
            elif "lig3" in arg_lower or "lig 3" in arg_lower or arg_lower == "3":
                tier_filter = 3
            elif "karisik" in arg_lower or "hepsi" in arg_lower or "tum" in arg_lower:
                tier_filter = None

            # Oran parametrelerini akıllı ayrıştır (örn: /oran 4.5, /oran 3-6, /6saat oran=5, /lig1 4.0)
            for token in arg.split():
                tl = token.lower()
                if tl.startswith("oran="):
                    val = tl.replace("oran=", "")
                    if "-" in val:
                        parts = val.split("-")
                        try:
                            target_range = (float(parts[0]), float(parts[1]))
                        except ValueError:
                            pass
                    else:
                        try:
                            target_odd = float(val)
                        except ValueError:
                            pass
                elif "-" in tl and re.match(r"^\d+(\.\d+)?-\d+(\.\d+)?$", tl):
                    parts = tl.split("-")
                    try:
                        target_range = (float(parts[0]), float(parts[1]))
                    except ValueError:
                        pass
                elif re.match(r"^\d+(\.\d+)?$", tl):
                    try:
                        v = float(tl)
                        if v >= 1.2:
                            target_odd = v
                    except ValueError:
                        pass

            self.send_message(
                chat_id,
                self.formatter.format_upcoming_coupons(
                    hours=6,
                    tier_filter=tier_filter,
                    target_odd=target_odd,
                    target_range=target_range,
                ),
                show_menu=True,
            )

        elif cmd == "/kupon":
            self.send_message(chat_id, self.formatter.format_coupons(), show_menu=True)

        elif cmd == "/sistem":
            self.send_message(chat_id, self.formatter.format_system_coupons(), show_menu=True)

        elif cmd == "/kasa":
            amt = float(arg) if arg.replace(".", "", 1).isdigit() else 1000.0
            self.send_message(chat_id, self.formatter.format_bankroll_plan(amt), show_menu=True)

        elif cmd == "/stratejiler":
            self.send_message(chat_id, self.formatter.format_methodology_explanation(), show_menu=True)

        elif cmd == "/analiz":
            self.send_message(chat_id, self.formatter.format_match_xray(arg), show_menu=True)

        elif cmd == "/yenile":
            matches = self.formatter.get_matches(force_refresh=True)
            self.send_message(chat_id, f"✅ Global bülten ve oranlar yenilendi! Aktif taranan maç sayısı: <b>{len(matches)}</b>", show_menu=True)

        elif cmd == "/oto_ac":
            self.subscribers.add(chat_id)
            self._save_subscribers()
            self.send_message(
                chat_id,
                "🔔 <b>Otomatik Bildirim Açıldı!</b>\nBültende güven skoru %85+ olan yeni Value/Taktik fırsatı yakalandığında otomatik mesaj alacaksınız.",
                show_menu=True,
            )

        elif cmd == "/oto_kapat":
            self.subscribers.discard(chat_id)
            self._save_subscribers()
            self.send_message(chat_id, "🔕 Otomatik bildirimler kapatıldı.", show_menu=True)

        else:
            # Komut değilse doğrudan takım araması olarak çalıştır
            self.send_message(chat_id, self.formatter.format_match_xray(text), show_menu=True)

    def _auto_notification_worker(self) -> None:
        """Arka planda her 30 dakikada bir bülteni tarayıp abonelere yüksek güvenli fırsatları gönderir."""
        while True:
            time.sleep(1800)
            if not self.subscribers:
                continue
            try:
                matches = self.formatter.get_matches(force_refresh=True)
                picks = self.formatter.analyzer.scan_bulletin(matches, "value_bet", top_n=3)
                new_items = []
                for p in picks:
                    code = p["analysis"]["match"]["code"]
                    if code not in self.sent_alerts and p["signal"]["confidence"] >= 82.0:
                        self.sent_alerts.add(code)
                        new_items.append(p)

                if new_items:
                    msg_lines = ["🚨 <b>OTOMATİK SİNYAL RADARI: YENİ VALUE FIRSATLARI</b>\n━━━━━━━━━━━━━━━━━━━━━━"]
                    for item in new_items:
                        m = item["analysis"]["match"]
                        s = item["signal"]
                        msg_lines.append(
                            f"⚽ <b>{m['home']} - {m['away']}</b> ({m['time']})\n"
                            f"👉 <b>{s['pick']}</b> @ <b>{s['odd']:.2f}</b> (Güven: %{s['confidence']:.1f} | +%{s['ev_pct']:.1f} EV)\n"
                            f"💡 <i>{s['reasons'][0]}</i>\n"
                        )
                    alert_text = "\n".join(msg_lines)
                    for sub_id in list(self.subscribers):
                        self.send_message(sub_id, alert_text, show_menu=False)
            except Exception as e:
                print(f"[Auto-Notify Error] {e}")

    def _register_bot_commands(self) -> None:
        commands = [
            {"command": "menu", "description": "📋 İnteraktif Taktik & Analiz Menüsü"},
            {"command": "canli", "description": "🔴 Canlı Fırsat Radarı (In-Play Sniping)"},
            {"command": "dusen", "description": "📉 Oran Düşüşü & Smart Money Takibi"},
            {"command": "sihirbaz", "description": "🧙‍♂️ Akıllı Kupon Sihirbazı (Örn: /sihirbaz mac=4 oran=3-8)"},
            {"command": "karne", "description": "📊 Model Başarı Karnesi & ROI Performansı"},
            {"command": "tuzak", "description": "⚠️ Ters Köşe & Tuzak Maç Radarı"},
            {"command": "tansiyon", "description": "🟨 Hakem & Tansiyon Radarı (Kart & Korner)"},
            {"command": "simulasyon", "description": "🎲 Monte Carlo 10.000 Simülasyon"},
            {"command": "yayinla", "description": "📢 Günlük VIP Bülteni Kanala/Sohbete Yayınla"},
            {"command": "kanal", "description": "📡 VIP Yayın Kanalı Belirle (/kanal @KanalAdi)"},
            {"command": "lig1", "description": "👑 6 Saatlik Lig 1 (Ana Ligler & Nesine 1)"},
            {"command": "lig2", "description": "🥈 6 Saatlik Lig 2 (2. Ligler & Kaliteli Ligler)"},
            {"command": "lig3", "description": "🥉 6 Saatlik Lig 3 (Alt Ligler & Diğerleri)"},
            {"command": "karisik", "description": "🎲 6 Saatlik Karışık (Tüm Ligler)"},
            {"command": "6saat", "description": "⏱️ Önümüzdeki 6 Saatin Kuponları"},
            {"command": "kupon", "description": "🎫 5 Kademeli Günlük Kuponlar (Garanti ➔ Bomba)"},
            {"command": "sistem", "description": "🎰 2 Absürt Oran Sistem Kuponu (8 & 10 Maç)"},
            {"command": "kasa", "description": "💰 Akıllı Kasa Dağıtım Planı (Örn: /kasa 1000)"},
            {"command": "kg", "description": "🎯 KG Var Taktiği"},
            {"command": "ms", "description": "🏆 MS Banko & Value Taktiği"},
            {"command": "ust25", "description": "🔥 2.5 Üst Gol Taktiği"},
            {"command": "alt", "description": "🛡️ 2.5 Alt (Savunma Duvarı) Taktiği"},
            {"command": "ust35", "description": "🚀 3.5 Üst Gol Fırtınası"},
            {"command": "arti6", "description": "💣 +6 Gol (Ultra Varyans / Kaos) Taktiği"},
            {"command": "ikiyarikg", "description": "⚡ İki Yarıda da KG Var (Evet/Evet) Taktiği"},
            {"command": "takimgol", "description": "🏠 Ev/Dep Her İki Yarıda Gol Atar"},
            {"command": "iyms", "description": "🔄 İY/MS Geri Dönüşler (1/2-2/1) & X/1-X/2"},
            {"command": "skor", "description": "🎯 Dixon-Coles Kesin Skor Sniper"},
            {"command": "kart", "description": "🟨 Kart & Korner Baskı Radarı"},
            {"command": "tgaralik", "description": "⚖️ 2-3 Gol & 4-5 Gol Aralığı"},
            {"command": "iyust", "description": "⏱️ İlk Yarı 1.5 Üst Erken Baskı Taktiği"},
            {"command": "kombo", "description": "💎 KG & 2.5 Üst / MS & 2.5 Üst Kombo"},
            {"command": "korner", "description": "📐 Korner 9.5 Üst Kanat Baskısı Taktiği"},
            {"command": "value", "description": "🧠 Sharp Value Bet (+EV Oran Sapması)"},
            {"command": "stratejiler", "description": "📚 Botun Kullandığı Global Modeller"},
            {"command": "oto_ac", "description": "🔔 Otomatik Value Bildirimlerini Aç"},
            {"command": "oto_kapat", "description": "🔕 Otomatik Bildirimleri Kapat"},
            {"command": "yenile", "description": "🔄 Global Bülteni ve Oranları Yenile"},
        ]
        try:
            me = self._api_call("getMe", {})
            bot_user = me.get("result", {}).get("username", "")
            print(f"✅ Bağlantı Başarılı! Bot Kullanıcı Adı: @{bot_user}")
            self._api_call("setMyCommands", {"commands": commands})
        except Exception as e:
            print(f"[Komut Menüsü Kayıt Uyarısı]: {e}")

    @staticmethod
    def _start_keep_alive_server() -> None:
        """Render.com / Bulut sunucular için $PORT üzerinde 7/24 uyanık tutma (health-check) web sunucusu."""
        port_str = os.environ.get("PORT", "").strip()
        if not port_str:
            return
        try:
            from http.server import BaseHTTPRequestHandler, HTTPServer

            port = int(port_str)

            class HealthHandler(BaseHTTPRequestHandler):
                def do_GET(self):
                    self.send_response(200)
                    self.send_header("Content-type", "text/plain; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(b"OK - Global Quant Betting Bot 7/24 Aktif!")

                def log_message(self, format, *args):
                    pass  # Log kirliliğini önle

            srv = HTTPServer(("0.0.0.0", port), HealthHandler)
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            print(f"🌐 Keep-Alive Web Sunucusu 0.0.0.0:{port} portunda başlatıldı (Render 7/24 Modu).")
        except Exception as e:
            print(f"[Keep-Alive Sunucu Uyarısı]: {e}")

    def run_polling(self) -> None:
        print("🚀 Telegram Bahis Tahmin Botu aktif! Mesajlar dinleniyor...")
        self._start_keep_alive_server()
        self._register_bot_commands()
        # Başlangıçta bülteni önbelleğe yükle
        self.formatter.get_matches()
        t = threading.Thread(target=self._auto_notification_worker, daemon=True)
        t.start()

        while True:
            try:
                res = self._api_call("getUpdates", {"offset": self.offset, "timeout": 25})
                for upd in res.get("result", []):
                    self.offset = upd["update_id"] + 1

                    if "message" in upd and "text" in upd["message"]:
                        chat_id = upd["message"]["chat"]["id"]
                        text = upd["message"]["text"]
                        self.handle_command(chat_id, text)

                    elif "callback_query" in upd:
                        cq = upd["callback_query"]
                        cq_id = cq["id"]
                        chat_id = cq["message"]["chat"]["id"]
                        data = cq.get("data", "")
                        try:
                            self._api_call("answerCallbackQuery", {
                                "callback_query_id": cq_id,
                                "text": "Tahminler hazırlanıyor... ⏳",
                            })
                        except Exception:
                            pass

                        try:
                            self._api_call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
                        except Exception:
                            pass

                        if data.startswith("strat:"):
                            s_key = data.split(":", 1)[1]
                            reply = self.formatter.format_strategy_picks(s_key)
                            self.send_message(chat_id, reply, show_menu=True)
                        elif data == "action:live_radar":
                            self.send_message(chat_id, self.formatter.format_live_radar(), show_menu=True)
                        elif data == "action:dropping":
                            self.send_message(chat_id, self.formatter.format_dropping_odds(), show_menu=True)
                        elif data == "action:wizard":
                            self.send_message(chat_id, self.formatter.format_wizard_coupon(), show_menu=True)
                        elif data == "action:scorecard":
                            self.send_message(chat_id, self.formatter.format_scorecard(), show_menu=True)
                        elif data == "action:trap":
                            self.send_message(chat_id, self.formatter.format_trap_radar(), show_menu=True)
                        elif data == "action:tension":
                            self.send_message(chat_id, self.formatter.format_tension_radar(), show_menu=True)
                        elif data == "action:monte_carlo":
                            self.send_message(chat_id, self.formatter.format_monte_carlo(), show_menu=True)
                        elif data == "action:vip_broadcast":
                            self.handle_command(chat_id, "/yayinla")
                        elif data == "action:upcoming_tier1":
                            self.send_message(chat_id, self.formatter.format_upcoming_coupons(hours=6, tier_filter=1), show_menu=True)
                        elif data == "action:upcoming_tier2":
                            self.send_message(chat_id, self.formatter.format_upcoming_coupons(hours=6, tier_filter=2), show_menu=True)
                        elif data == "action:upcoming_tier3":
                            self.send_message(chat_id, self.formatter.format_upcoming_coupons(hours=6, tier_filter=3), show_menu=True)
                        elif data in ("action:upcoming_all", "action:upcoming_4h"):
                            self.send_message(chat_id, self.formatter.format_upcoming_coupons(hours=6, tier_filter=None), show_menu=True)
                        elif data.startswith("action:uptarget:"):
                            rng = data.replace("action:uptarget:", "")
                            p_rng = rng.split("_")
                            try:
                                r_min, r_max = float(p_rng[0]), float(p_rng[1])
                                self.send_message(
                                    chat_id,
                                    self.formatter.format_upcoming_coupons(hours=6, tier_filter=1, target_range=(r_min, r_max)),
                                    show_menu=True,
                                )
                            except Exception:
                                pass
                        elif data == "action:coupons":
                            self.send_message(chat_id, self.formatter.format_coupons(), show_menu=True)
                        elif data == "action:system":
                            self.send_message(chat_id, self.formatter.format_system_coupons(), show_menu=True)
                        elif data == "action:kasa":
                            self.send_message(chat_id, self.formatter.format_bankroll_plan(1000.0), show_menu=True)
                        elif data == "action:models":
                            self.send_message(chat_id, self.formatter.format_methodology_explanation(), show_menu=True)
                        elif data == "action:refresh":
                            ms = self.formatter.get_matches(force_refresh=True)
                            self.send_message(
                                chat_id,
                                f"✅ Bülten güncellendi! Toplam <b>{len(ms)}</b> global maç analiz edildi.",
                                show_menu=True,
                            )
            except KeyboardInterrupt:
                print("\nBot durduruldu.")
                break
            except Exception as e:
                print(f"[Polling Hatası - 5 sn sonra yeniden denenecek]: {e}")
                time.sleep(5)


def strip_html(content: Union[str, List[str]]) -> str:
    """Terminal çıktısı için HTML etiketlerini temizler."""
    if isinstance(content, list):
        text = "\n\n".join(content)
    else:
        text = str(content)
    return re.sub(r"<[^>]+>", "", text)


def run_cli_demo(formatter: BettingBotFormatter, strategy: Optional[str] = None, team: Optional[str] = None) -> None:
    if team:
        print(strip_html(formatter.format_match_xray(team)))
        return

    if strategy:
        print(strip_html(formatter.format_strategy_picks(strategy, top_n=5)))
        return

    # Varsayılan kapsamlı demo raporu
    for skey in ["kg_var", "ms_banko", "ust_25", "alt_taktigi", "arti_6_gol", "iki_yari_kg", "iyms_surpriz", "kesin_skor", "value_bet"]:
        print(strip_html(formatter.format_strategy_picks(skey, top_n=3)))
        print("\n")
    print(strip_html(formatter.format_coupons()))
    print("\n" + strip_html(formatter.format_system_coupons()))
    print("\n" + strip_html(formatter.format_bankroll_plan(1000.0)))


def main() -> None:
    parser = argparse.ArgumentParser(description="Global Bahis Tahmin Telegram Botu & Analiz Motoru")
    parser.add_argument("--demo", action="store_true", help="Terminal üzerinde tüm taktiklerin canlı bülten tahminlerini gösterir")
    parser.add_argument("--strateji", type=str, help="Belirli bir stratejiyi çalıştırır (kg_var, ms_banko, ust_25, ust_35, arti_6_gol, iki_yari_kg, iy_ust, kombo, korner, value_bet)")
    parser.add_argument("--analiz", type=str, help="Belirli bir takımın maçını derinlemesine analiz eder (Örn: --analiz Chelsea)")
    parser.add_argument("--token", type=str, help="Telegram Bot Token (Verilmezse TELEGRAM_BOT_TOKEN veya config.json kullanılır)")
    args = parser.parse_args()

    fetcher = GlobalMatchDataFetcher()
    analyzer = MatchQuantitativeAnalyzer()
    formatter = BettingBotFormatter(fetcher, analyzer)

    if args.demo or args.strateji or args.analiz:
        run_cli_demo(formatter, strategy=args.strateji, team=args.analiz)
        return

    token = args.token or load_bot_token()
    if not token:
        print("⚠️ Telegram Bot Token bulunamadı (TELEGRAM_BOT_TOKEN veya config.json). Canlı Terminal Demo Modu çalıştırılıyor...\n")
        run_cli_demo(formatter)
        print("\n💡 Telegram Botunu başlatmak için:")
        print("   export TELEGRAM_BOT_TOKEN='SİZİN_TOKENINIZ' && python3 bot.py")
        return

    bot = TelegramBettingBot(token, formatter)
    bot.run_polling()


if __name__ == "__main__":
    main()
