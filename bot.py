#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
4 Saatlik 3'lü Sniper Kupon Sistemi (200₺ ➔ 5 Oran | 100₺ ➔ 10 Oran | 50₺ ➔ 20 Oran)
Komutlar:
  /start, /menu  -> 4 Saatlik 3'lü Kupon Sistemi Ana Menüsü
  /sistem, /kupon-> 🔥 Önümüzdeki 4 Saat İçin 3'lü Kupon Paketi (200₺ 5x, 100₺ 10x, 50₺ 20x)
  /analig        -> 👑 Sadece Ana Ligler (Lig 1) İçin 4 Saatlik 3'lü Kupon Paketi
  /5             -> 🟢 200 TL ➔ 5 Oran (2-3 Maçlık Banko Taktik Kuponu)
  /10            -> 🟡 100 TL ➔ 10 Oran (3-4 Maçlık İdeal Taktik Kuponu)
  /20            -> 🔴 50 TL ➔ 20 Oran (3-4 Maçlık Sniper Vurgun Kuponu)
  /alternatif    -> 🔄 Farklı Maçlardan Oluşan Alternatif 3'lü Kupon Paketi
  /karne         -> 📊 Kupon Başarı Karnesi & Gerçek TL Kasa ROE Takibi
  /yenile        -> ⚡ Bülteni ve Oranları Canlı Yenile
"""

import argparse
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Set, Union

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
    """3'lü kupon sisteminin (200₺ / 100₺ / 50₺) önerdiği kuponları kaydedip sonuçlarını otomatik puanlayan takip motoru."""

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
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self._records[-500:], f, ensure_ascii=False)
        except Exception:
            pass

    def record_coupon(
        self,
        coupon_title: str,
        picks: List[Dict[str, Any]],
        total_odd: Optional[float] = None,
        stake_tl: int = 100,
    ) -> None:
        """Kuponu, kupon mislini (200₺/100₺/50₺) ve içerdiği tüm maçları tek bir coupon_id altında kaydeder."""
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
                    "coupon_stake": int(stake_tl),
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

    def update_with_results(self, finished_map: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        """Bitmiş maç skorlarıyla bekleyen kuponları puanlar ve gerçek TL bazlı Kupon ROE hesaplar."""
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
                    r["status"] = "UNVERIFIED"
                r["final_score"] = f"{hg}-{ag}"

        self._save()

        coupons_dict: Dict[str, Dict[str, Any]] = {}
        for r in self._records:
            cid = r.get("coupon_id") or r.get("coupon", "legacy")
            if cid not in coupons_dict:
                coupons_dict[cid] = {
                    "id": cid,
                    "title": r.get("coupon", "Kupon"),
                    "odd": float(r.get("coupon_odd", 1.0)),
                    "stake": int(r.get("coupon_stake", 100)),
                    "picks": [],
                }
            coupons_dict[cid]["picks"].append(r)

        evaluated_coupons = []
        won_coupons = []
        lost_coupons = []
        pending_coupons = []

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

        coupon_staked_tl = sum(c["stake"] for c in evaluated_coupons)
        coupon_returned_tl = sum(round(c["odd"] * c["stake"], 2) for c in won_coupons)
        coupon_net_profit_tl = round(coupon_returned_tl - coupon_staked_tl, 2)
        coupon_roe_pct = ((coupon_returned_tl - coupon_staked_tl) / coupon_staked_tl * 100) if coupon_staked_tl > 0 else 0.0
        coupon_win_rate = (len(won_coupons) / len(evaluated_coupons) * 100) if evaluated_coupons else 0.0

        evaluated_picks = [r for r in self._records if r["status"] in ("WON", "LOST")]
        won_picks = [r for r in evaluated_picks if r["status"] == "WON"]
        lost_picks = [r for r in evaluated_picks if r["status"] == "LOST"]
        pending_picks = [r for r in self._records if r["status"] == "PENDING"]
        pick_win_rate = (len(won_picks) / len(evaluated_picks) * 100) if evaluated_picks else 0.0

        return {
            "coupon_total": len(evaluated_coupons),
            "coupon_won": len(won_coupons),
            "coupon_lost": len(lost_coupons),
            "coupon_pending": len(pending_coupons),
            "coupon_win_rate": round(coupon_win_rate, 1),
            "coupon_staked_tl": coupon_staked_tl,
            "coupon_returned_tl": round(coupon_returned_tl, 1),
            "coupon_net_profit_tl": coupon_net_profit_tl,
            "coupon_roe_pct": round(coupon_roe_pct, 1),
            "pick_total": len(evaluated_picks),
            "pick_won": len(won_picks),
            "pick_lost": len(lost_picks),
            "pick_pending": len(pending_picks),
            "pick_win_rate": round(pick_win_rate, 1),
            "recent": evaluated_picks[-8:],
        }


class BettingBotFormatter:
    """4 Saatlik 3'lü Sniper Kupon Sistemini (200₺ 5x / 100₺ 10x / 50₺ 20x) şık formatta üretir."""

    def __init__(self, fetcher: GlobalMatchDataFetcher, analyzer: MatchQuantitativeAnalyzer):
        self.fetcher = fetcher
        self.analyzer = analyzer
        self.tracker = PredictionTracker()
        self._matches_cache: List[Dict[str, Any]] = []
        self._last_load_ts: float = 0.0

    def get_matches(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        now = time.time()
        if force_refresh or not self._matches_cache or (now - self._last_load_ts > 600):
            if force_refresh:
                self.fetcher.cache_ttl = 0
            self._matches_cache = self.fetcher.fetch_matches(max_matches=550)
            self.fetcher.cache_ttl = 600
            self._last_load_ts = now
        return self._matches_cache

    @staticmethod
    def _format_coupon_card(c: Dict[str, Any]) -> str:
        """Tek bir kupon kartını (200₺ 5x, 100₺ 10x veya 50₺ 20x) detaylı olarak formatlar."""
        title = _safe_html(c.get("title", "KUPON"))
        subtitle = _safe_html(c.get("subtitle", ""))
        stake_tl = int(c.get("fixed_stake", c.get("stake_tl", 100)))
        target_odd = float(c.get("target_odd", 5.0))
        total_odd = float(c.get("total_odd", 0.0))
        potential_win = float(c.get("expected_payout", c.get("potential_win_tl", round(total_odd * stake_tl, 2))))
        joint_prob = float(c.get("joint_prob", c.get("joint_prob_pct", 0.0)))
        min_leg_prob = float(c.get("min_leg_prob", c.get("min_leg_prob_pct", 0.0)))
        picks = c.get("picks", [])

        lines = [
            f"<b>{title}</b>",
            f"<i>{subtitle}</i>",
            "━━━━━━━━━━━━━━━━━━━━━━",
        ]

        if not picks:
            lines.append("⚠️ Bu saat diliminde bu hedef orana uygun güvenilir maç kombinasyonu bulunamadı.")
            return "\n".join(lines)

        for idx, p in enumerate(picks, 1):
            m_name = _safe_html(p["match"])
            lg = _safe_html(p.get("league", ""))
            tm = _safe_html(p.get("time", ""))
            code = _safe_html(p.get("code", ""))
            pick_name = _safe_html(p["pick"])
            odd = float(p["odd"])
            prob = float(p["prob"])
            ev_pct = float(p.get("ev_pct", 0.0))
            lam_h = p.get("lam_home", "-")
            lam_a = p.get("lam_away", "-")
            top_sc = _safe_html(p.get("top_scores", ""))
            reason = _safe_html(p.get("reason", ""))
            tier_tag = _safe_html(p.get("tier_label", "⚽ Bülten"))
            role_badge = _safe_html(p.get("role_badge", ""))
            ev_tag = f" | +%{ev_pct:.1f} Değer" if ev_pct > 0 else ""
            role_line = f"   {role_badge}\n" if role_badge else ""

            lines.append(
                f"<b>{idx}) ⚽ {m_name}</b>\n"
                f"   🏆 {lg} ({tier_tag}) | 🕒 <b>{tm}</b> | Kod: <code>#{code}</code>\n"
                f"{role_line}"
                f"   👉 <b>TERCİH: {pick_name}</b>  —  💰 <b>Oran: {odd:.2f}</b>\n"
                f"   📈 <b>Model İhtimali:</b> %{prob:.1f}{ev_tag} | 🧮 <b>xG:</b> Ev {lam_h} - Dep {lam_a}\n"
                f"   🎲 <b>En Olası Skorlar:</b> {top_sc}\n"
                f"   📌 <i>{reason}</i>"
            )
            lines.append("──────────────────────")

        lines.append(
            f"📦 <b>Maç Sayısı:</b> <b>{len(picks)} Maç</b> (Minimum Maç &amp; Maksimum İsabet)\n"
            f"🔥 <b>Toplam Kupon Oranı:</b> <b>{total_odd:.2f}</b> (Hedef: ~{target_odd:.0f}.00)\n"
            f"💵 <b>Basılacak Tutar:</b> <b>{stake_tl} TL</b>\n"
            f"🏆 <b>Kupon Tutarsa Net Kazanç:</b> <b>{potential_win:,.2f} TL</b>\n"
            f"🛡️ <b>En Düşük Maç Güveni:</b> %{min_leg_prob:.1f} | <b>Birleşik Kupon İhtimali:</b> %{joint_prob:.1f}"
        )
        return "\n".join(lines)

    def format_trinity_system(
        self,
        hours: int = 4,
        elite_only: bool = False,
        alt_mode: bool = False,
    ) -> List[str]:
        """
        Önümüzdeki 4 saat için 3'lü Sniper Kupon Sistemini oluşturur:
          1) 200 TL ➔ 5 Oran (~1.000 TL Kazanç)
          2) 100 TL ➔ 10 Oran (~1.000 TL Kazanç)
          3) 50 TL  ➔ 20 Oran (~1.000 TL Kazanç)
        Telegram karakter limitine takılmamak ve okunaklılığı maksimumda tutmak için
        Özet + Kuponlar halinde temiz mesaj dizisi döndürür.
        """
        matches = self.get_matches()
        if alt_mode:
            primary = self.analyzer.build_trinity_4h_system(matches, hours=hours, elite_only=elite_only)
            primary_coupons_dict = primary.get("coupons", {})
            primary_list = list(primary_coupons_dict.values()) if isinstance(primary_coupons_dict, dict) else primary_coupons_dict
            primary_codes: Set[str] = set()
            for c in primary_list:
                for p in c.get("picks", [])[:1]:
                    if p.get("code"):
                        primary_codes.add(p["code"])

            c5 = self.analyzer.build_sniper_4h_coupon(
                matches, hours=hours, target_odd=5.0, fixed_stake=200.0,
                elite_only=elite_only, exclude_codes=set(primary_codes)
            )
            used_alt = set(primary_codes) | {p["code"] for p in c5.get("picks", []) if p.get("code")}
            c10 = self.analyzer.build_sniper_4h_coupon(
                matches, hours=hours, target_odd=10.0, fixed_stake=100.0,
                elite_only=elite_only, exclude_codes=used_alt
            )
            used_alt |= {p["code"] for p in c10.get("picks", []) if p.get("code")}
            c20 = self.analyzer.build_sniper_4h_coupon(
                matches, hours=hours, target_odd=20.0, fixed_stake=50.0,
                elite_only=elite_only, exclude_codes=used_alt
            )
            c5["title"] = "🟢 1. ALTERNATİF KUPON: 200 TL ➔ 5 ORAN"
            c5["subtitle"] = "Farklı maçlarla 2-3 maçta ~1.000 TL hedefleyen alternatif ana kasa kuponu."
            c10["title"] = "🟡 2. ALTERNATİF KUPON: 100 TL ➔ 10 ORAN"
            c10["subtitle"] = "Farklı maçlarla 3-4 maçta ~1.000 TL hedefleyen alternatif orta çarpan kuponu."
            c20["title"] = "🔴 3. ALTERNATİF KUPON: 50 TL ➔ 20 ORAN"
            c20["subtitle"] = "Farklı maçlarla 3-4 maçta ~1.000 TL hedefleyen alternatif sniper vurgun kuponu."
            coupons = [c5, c10, c20]
            w_hours = c5.get("window_hours", hours)
            start_t = c5.get("start_time", "")
            end_t = c5.get("end_time", "")
            cnt = c5.get("match_count", 0)
        else:
            sys_data = self.analyzer.build_trinity_4h_system(matches, hours=hours, elite_only=elite_only)
            c_dict = sys_data["coupons"]
            coupons = [c_dict["kupon_5x"], c_dict["kupon_10x"], c_dict["kupon_20x"]]
            w_hours = sys_data["window_hours"]
            start_t = sys_data["start_time"]
            end_t = sys_data["end_time"]
            cnt = sys_data["match_count"]

        for c in coupons:
            if c.get("picks"):
                self.tracker.record_coupon(
                    c.get("title", "Sniper Kupon"),
                    c["picks"],
                    total_odd=c.get("total_odd"),
                    stake_tl=int(c.get("fixed_stake", 100)),
                )

        mode_badge = "👑 SADECE ANA LİGLER (LİG 1)" if elite_only else ("🔄 ALTERNATİF 3'LÜ PAKET" if alt_mode else "🔥 ÖNÜMÜZDEKİ 4 SAAT — 3'LÜ KUPON SİSTEMİ")

        header_msg = (
            f"🎯 <b>{mode_badge}</b>\n"
            f"🕒 <b>Zaman Penceresi:</b> <b>{start_t} - {end_t}</b> ({w_hours} Saatlik Dilim)\n"
            f"📊 <b>Taranan Aktif Maç:</b> <b>{cnt} Maç</b> | <b>Dixon-Coles 9x9 Taktik Motoru</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "💰 <b>KASA &amp; HEDEF STRATEJİSİ (350 TL ➔ ~3.000 TL):</b>\n"
            "• 🟢 <b>Kupon 1:</b> <code>200 TL</code> ➔ <b>~5 Oran</b> (~1.000 TL Hedef | 2-3 Maç)\n"
            "• 🟡 <b>Kupon 2:</b> <code>100 TL</code> ➔ <b>~10 Oran</b> (~1.000 TL Hedef | 3 Maç)\n"
            "• 🔴 <b>Kupon 3:</b> <code>50 TL</code>  ➔ <b>~20 Oran</b> (~1.000 TL Hedef | 3-4 Maç)\n"
            "──────────────────────\n"
            "🛡️ <b>Altın Kurallar:</b>\n"
            "• <b>En Az Maç, En Yüksek Olasılık:</b> 8-10-20 maçlık kupon, +6 Gol veya İki Yarı KG (Evet/Evet) <b>kesinlikle eklenmez</b>.\n"
            "• <b>Tam Taktik Cephaneliği:</b> MS &amp; Üst/Alt, Ev/Dep 1.5 Üst, Her İki Yarıda Gol, İY 1.5 Üst, Handikap ve 2-3 Gol kombinasyonlarıyla en az maçta hedef orana ulaşılır.\n"
            "• <b>Maç İzolasyonu:</b> 3 kuponun tamamı birbirinden <b>farklı maçlardan</b> oluşur; tek bir maç sürprizi diğer kuponları yakmaz!\n"
            "💡 <i>Sadece tek bir kuponumuz bile tutsa ~1.000 TL geri dönüş ile tüm 350 TL kasa maliyeti yaklaşık 3'e katlanır!</i>"
        )

        messages = [header_msg]
        for c in coupons:
            messages.append(self._format_coupon_card(c))

        return messages

    def format_single_pillar(
        self,
        target_odd: float,
        stake_tl: int,
        hours: int = 4,
        elite_only: bool = False,
    ) -> str:
        """Kullanıcı sadece 5 Oran (200₺), 10 Oran (100₺) veya 20 Oran (50₺) istediğinde tek kupon üretir."""
        matches = self.get_matches()
        c = self.analyzer.build_sniper_4h_coupon(
            matches,
            hours=hours,
            target_odd=target_odd,
            fixed_stake=float(stake_tl),
            elite_only=elite_only,
        )
        if target_odd <= 6.0:
            c["title"] = f"🟢 ÖNÜMÜZDEKİ {c['window_hours']} SAAT: {stake_tl} TL ➔ 5 ORAN KUPONU"
            c["subtitle"] = "En az maçla (2-3 maç) ~1.000 TL hedefleyen maksimum güvenli taktik kombinasyonu"
        elif target_odd <= 12.0:
            c["title"] = f"🟡 ÖNÜMÜZDEKİ {c['window_hours']} SAAT: {stake_tl} TL ➔ 10 ORAN KUPONU"
            c["subtitle"] = "3-4 maçta taktiksel kombo marketlerle ~1.000 TL hedefleyen ideal çarpan kuponu"
        else:
            c["title"] = f"🔴 ÖNÜMÜZDEKİ {c['window_hours']} SAAT: {stake_tl} TL ➔ 20 ORAN SNIPER KUPONU"
            c["subtitle"] = "Çok maç doldurmadan (3-4 maçla) yüksek değerli taktiklerle ~1.000 TL vurgun kuponu"

        if c.get("picks"):
            self.tracker.record_coupon(
                c["title"],
                c["picks"],
                total_odd=c.get("total_odd"),
                stake_tl=stake_tl,
            )

        header = (
            f"🕒 <b>Zaman Penceresi:</b> {c['start_time']} - {c['end_time']} ({c['window_hours']} Saat) | "
            f"<b>Taranan Maç:</b> {c['match_count']}\n\n"
        )
        return header + self._format_coupon_card(c)

    def format_scorecard(self) -> str:
        """Kuponların (200₺ / 100₺ / 50₺) geçmiş başarı ve gerçek TL ROE raporunu oluşturur."""
        scores_data = self.fetcher.fetch_mackolik_scores()
        fin_map = scores_data.get("finished", {})
        stats = self.tracker.update_with_results(fin_map)

        c_tot = stats["coupon_total"]
        c_won = stats["coupon_won"]
        c_lost = stats["coupon_lost"]
        c_pend = stats["coupon_pending"]
        c_win_rate = stats["coupon_win_rate"]
        staked_tl = stats["coupon_staked_tl"]
        ret_tl = stats["coupon_returned_tl"]
        net_tl = stats["coupon_net_profit_tl"]
        c_roe = stats["coupon_roe_pct"]

        p_tot = stats["pick_total"]
        p_won = stats["pick_won"]
        p_lost = stats["pick_lost"]
        p_pend = stats["pick_pending"]
        p_win_rate = stats["pick_win_rate"]

        lines = [
            "📊 <b>3'LÜ SNIPER SİSTEMİ KUPON KARNESİ &amp; KASA ROE</b>",
            "<i>Önerilen 200₺ (5x), 100₺ (10x) ve 50₺ (20x) kuponların resmi maç sonu skorlarıyla performansı:</i>",
            "━━━━━━━━━━━━━━━━━━━━━━",
            "🎫 <b>1. KUPON BAZLI KASA PERFORMANSI:</b>",
            f"  • <b>Sonuçlanan Kupon:</b> {c_tot} Adet (✅ {c_won} Tutan | ❌ {c_lost} Yatan)",
            f"  • 🎯 <b>Kupon İsabet Oranı:</b> %{c_win_rate:.1f}",
            f"  • 💵 <b>Toplam Basılan Kasa:</b> {staked_tl:,.0f} TL",
            f"  • 🏆 <b>Toplam Dönen Kazanç:</b> {ret_tl:,.1f} TL",
            f"  • 💰 <b>Net Kar / Zarar:</b> <b>{net_tl:+,.1f} TL</b> (ROE: <b>%{c_roe:+.1f}</b>)",
            f"  • ⏳ <b>Maçları Devam Eden Kupon:</b> {c_pend} Adet",
            "──────────────────────",
            "⚽ <b>2. TEKİL MAÇ TERCİH İSABETİ:</b>",
            f"  • <b>Sonuçlanan Maç:</b> {p_tot} Maç (✅ {p_won} Kazanan | ❌ {p_lost} Kaybeden)",
            f"  • 🎯 <b>Tekil Maç Başarı Oranı:</b> <b>%{p_win_rate:.1f}</b>",
            f"  • ⏳ <b>Bekleyen Maç:</b> {p_pend} Maç",
            "──────────────────────",
        ]

        recent = stats.get("recent", [])
        if recent:
            lines.append("🏁 <b>Son Sonuçlanan Maç Tercihleri:</b>")
            for r in recent[-6:]:
                emoji = "✅" if r["status"] == "WON" else "❌"
                sc = r.get("final_score") or "-"
                lines.append(
                    f"  {emoji} <b>{_safe_html(r['match'])}</b> ({sc}) ➔ {_safe_html(r['pick'])} @ <b>{r['odd']:.2f}</b>"
                )
        else:
            lines.append("💡 <i>Henüz sonuçlanmış maç yok. Kuponlardaki maçlar bittikçe karne otomatik güncellenir.</i>")

        return "\n".join(lines)

    def format_match_xray(self, query: str) -> str:
        """Kullanıcı bir takım adı yazdığında o maç için en mantıklı taktiksel tercihleri gösterir."""
        query_clean = query.strip().lower()
        if not query_clean:
            return "⚠️ Lütfen bir takım adı yazın veya menüden <b>4 Saatlik 3'lü Sistem</b> butonuna tıklayın."

        matches = self.get_matches()
        found = [
            m for m in matches
            if query_clean in m["home"].lower() or query_clean in m["away"].lower() or query_clean == m["code"]
        ]
        if not found:
            return f"❌ <b>'{_safe_html(query)}'</b> aramasına uygun bültende aktif maç bulunamadı."

        m = found[0]
        a = self.analyzer.analyze_match(m)
        arsenal = self.analyzer._extract_tactical_arsenal(m, a)
        top_sc = " | ".join(f"{sc} (%{pr})" for sc, pr in a["top_scores"])

        lines = [
            f"🔬 <b>MAÇ TAKTİK RÖNTGENİ: {_safe_html(m['home'])} - {_safe_html(m['away'])}</b>",
            f"🏆 {_safe_html(m['league'])} | 🕒 {m['date']} {m['time']} | Kod: <code>#{m['code']}</code>",
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"🧮 <b>Gol Beklentisi (xG):</b> Ev <b>{a['lam_home']}</b> - Dep <b>{a['lam_away']}</b> (Toplam: <b>{a['lam_total']}</b>)",
            f"🎲 <b>En Olası 3 Skor:</b> <b>{top_sc}</b>",
            "──────────────────────",
            "🎯 <b>BU MAÇ İÇİN EN İYİ TAKTİK TERCİHLER (5x / 10x / 20x Uygunluğuna Göre):</b>",
        ]

        if not arsenal:
            lines.append("  ⚠️ Bu maç için güvenli eşiği geçen açık oran bulunamadı.")
        else:
            for idx, opt in enumerate(arsenal[:5], 1):
                r_txt = opt["reasons"][0] if opt.get("reasons") else ""
                lines.append(
                    f"  {idx}) 👉 <b>{_safe_html(opt['pick'])}</b> — Oran: <b>{opt['odd']:.2f}</b> (Model: <b>%{opt['prob']:.1f}</b>)\n"
                    f"     └ <i>{_safe_html(r_txt)}</i>"
                )

        return "\n".join(lines)


class TelegramBettingBot:
    """Sadece 4 Saatlik 3'lü Sniper Sistemine (200₺ 5x | 100₺ 10x | 50₺ 20x) odaklanmış Telegram Bot İstemcisi."""

    STRATEGY_BUTTONS = [
        [
            {"text": "🔥 4 Saatlik 3'lü Sistem (200₺ 5x | 100₺ 10x | 50₺ 20x)", "callback_data": "action:trinity_all"},
        ],
        [
            {"text": "👑 Sadece Ana Ligler (Lig 1): 4 Saatlik 3'lü Sistem", "callback_data": "action:trinity_elite"},
        ],
        [
            {"text": "🟢 200 TL ➔ 5 Oran Kupon", "callback_data": "action:pillar_5"},
            {"text": "🟡 100 TL ➔ 10 Oran Kupon", "callback_data": "action:pillar_10"},
        ],
        [
            {"text": "🔴 50 TL ➔ 20 Oran Kupon", "callback_data": "action:pillar_20"},
            {"text": "🔄 Alternatif 3'lü Sistem", "callback_data": "action:trinity_alt"},
        ],
        [
            {"text": "📊 Kupon Karnesi & Kasa ROE", "callback_data": "action:scorecard"},
            {"text": "⚡ Bülteni Canlı Yenile", "callback_data": "action:refresh"},
        ],
    ]

    def __init__(self, token: str, formatter: BettingBotFormatter):
        self.token = token
        self.formatter = formatter
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.offset = 0
        self.subscribers: Set[int] = self._load_subscribers()

    def _load_subscribers(self) -> Set[int]:
        if os.path.exists(SUBSCRIBERS_PATH):
            try:
                with open(SUBSCRIBERS_PATH, "r", encoding="utf-8") as f:
                    return set(json.load(f))
            except Exception:
                pass
        return set()

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

        if cmd in ("/start", "/menu", "/help"):
            welcome = (
                "🎯 <b>4 SAATLİK 3'LÜ SNIPER KUPON SİSTEMİNE HOŞ GELDİNİZ!</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "Gereksiz tüm menüler kaldırıldı. Sistem artık <b>önümüzdeki 4 saatlik</b> maçları "
                "<b>Dixon-Coles 9x9 Skor Matrisi</b> ve <b>29+ Taktiksel Market</b> ile tarayarak "
                "en az maçla <b>~1.000 TL hedefleyen 3 izole kupon</b> üretir:\n\n"
                "🟢 <b>1. Kupon:</b> <code>200 TL</code> ➔ <b>5 Oran</b> (~1.000 TL | 2-3 Maç)\n"
                "🟡 <b>2. Kupon:</b> <code>100 TL</code> ➔ <b>10 Oran</b> (~1.000 TL | 3 Maç)\n"
                "🔴 <b>3. Kupon:</b> <code>50 TL</code>  ➔ <b>20 Oran</b> (~1.000 TL | 3-4 Maç)\n\n"
                "🛡️ <b>Sistemin Kuralları:</b>\n"
                "• Asla +6 Gol, İki Yarı KG (Evet/Evet) veya 10-20 maçlık çöp kupon yapmaz.\n"
                "• MS &amp; 1.5/2.5 Üst, MS &amp; 3.5/4.5 Alt, Ev/Dep 1.5 Üst, Her İki Yarıda Gol, İY 1.5 Üst, 2-3 Gol ve Handikap gibi tüm taktikleri kullanarak <b>en olası ve en az maçla</b> işi bitirir.\n"
                "• 3 kuponun maçları tamamen <b>birbirinden farklıdır</b> (Tek maç yatarsa diğer kuponlar etkilenmez).\n\n"
                "👇 <b>Aşağıdaki butonlardan kuponlarınızı hemen alabilirsiniz:</b>"
            )
            self.send_message(chat_id, welcome, show_menu=True)

        elif cmd in ("/sistem", "/kupon", "/4saat", "/trinity"):
            self.send_message(chat_id, self.formatter.format_trinity_system(hours=4, elite_only=False), show_menu=True)

        elif cmd in ("/analig", "/lig1", "/elit"):
            self.send_message(chat_id, self.formatter.format_trinity_system(hours=4, elite_only=True), show_menu=True)

        elif cmd in ("/alternatif", "/alt"):
            self.send_message(chat_id, self.formatter.format_trinity_system(hours=4, elite_only=False, alt_mode=True), show_menu=True)

        elif cmd in ("/5", "/5oran", "/200"):
            self.send_message(chat_id, self.formatter.format_single_pillar(target_odd=5.0, stake_tl=200, hours=4), show_menu=True)

        elif cmd in ("/10", "/10oran", "/100"):
            self.send_message(chat_id, self.formatter.format_single_pillar(target_odd=10.0, stake_tl=100, hours=4), show_menu=True)

        elif cmd in ("/20", "/20oran", "/50"):
            self.send_message(chat_id, self.formatter.format_single_pillar(target_odd=20.0, stake_tl=50, hours=4), show_menu=True)

        elif cmd in ("/karne", "/roe", "/rapor"):
            self.send_message(chat_id, self.formatter.format_scorecard(), show_menu=True)

        elif cmd == "/yenile":
            matches = self.formatter.get_matches(force_refresh=True)
            self.send_message(
                chat_id,
                f"⚡ <b>Bülten ve oranlar canlı yenilendi!</b>\nAktif taranan maç sayısı: <b>{len(matches)}</b>",
                show_menu=True,
            )

        elif cmd == "/analiz":
            self.send_message(chat_id, self.formatter.format_match_xray(arg), show_menu=True)

        else:
            self.send_message(chat_id, self.formatter.format_match_xray(text), show_menu=True)

    def _register_bot_commands(self) -> None:
        commands = [
            {"command": "sistem", "description": "🔥 4 Saatlik 3'lü Sistem (200₺ 5x | 100₺ 10x | 50₺ 20x)"},
            {"command": "analig", "description": "👑 Sadece Ana Ligler (Lig 1): 4 Saatlik 3'lü Sistem"},
            {"command": "5", "description": "🟢 200 TL ➔ 5 Oran Kupon (2-3 Maç)"},
            {"command": "10", "description": "🟡 100 TL ➔ 10 Oran Kupon (3-4 Maç)"},
            {"command": "20", "description": "🔴 50 TL ➔ 20 Oran Sniper Kupon (3-4 Maç)"},
            {"command": "alternatif", "description": "🔄 Farklı Maçlardan Alternatif 3'lü Kupon Paketi"},
            {"command": "karne", "description": "📊 Kupon Başarı Karnesi & Kasa ROE"},
            {"command": "yenile", "description": "⚡ Bülteni ve Oranları Canlı Yenile"},
            {"command": "menu", "description": "📋 Ana Menüyü Göster"},
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
        """Render.com / Bulut sunucular için $PORT üzerinde 7/24 uyanık tutma web sunucusu."""
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
                    self.wfile.write(b"OK - 4H Trinity Sniper Bot 7/24 Aktif!")

                def log_message(self, format, *args):
                    pass

            srv = HTTPServer(("0.0.0.0", port), HealthHandler)
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            print(f"🌐 Keep-Alive Web Sunucusu 0.0.0.0:{port} portunda başlatıldı.")
        except Exception as e:
            print(f"[Keep-Alive Sunucu Uyarısı]: {e}")

    def run_polling(self) -> None:
        print("🚀 4 Saatlik 3'lü Sniper Kupon Botu aktif! Mesajlar dinleniyor...")
        self._start_keep_alive_server()
        self._register_bot_commands()
        self.formatter.get_matches()

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
                                "text": "4 saatlik kuponlar hesaplanıyor... ⏳",
                            })
                        except Exception:
                            pass

                        try:
                            self._api_call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
                        except Exception:
                            pass

                        if data == "action:trinity_all":
                            self.send_message(chat_id, self.formatter.format_trinity_system(hours=4, elite_only=False), show_menu=True)
                        elif data == "action:trinity_elite":
                            self.send_message(chat_id, self.formatter.format_trinity_system(hours=4, elite_only=True), show_menu=True)
                        elif data == "action:trinity_alt":
                            self.send_message(chat_id, self.formatter.format_trinity_system(hours=4, elite_only=False, alt_mode=True), show_menu=True)
                        elif data == "action:pillar_5":
                            self.send_message(chat_id, self.formatter.format_single_pillar(target_odd=5.0, stake_tl=200, hours=4), show_menu=True)
                        elif data == "action:pillar_10":
                            self.send_message(chat_id, self.formatter.format_single_pillar(target_odd=10.0, stake_tl=100, hours=4), show_menu=True)
                        elif data == "action:pillar_20":
                            self.send_message(chat_id, self.formatter.format_single_pillar(target_odd=20.0, stake_tl=50, hours=4), show_menu=True)
                        elif data == "action:scorecard":
                            self.send_message(chat_id, self.formatter.format_scorecard(), show_menu=True)
                        elif data == "action:refresh":
                            ms = self.formatter.get_matches(force_refresh=True)
                            self.send_message(
                                chat_id,
                                f"⚡ Bülten güncellendi! Toplam <b>{len(ms)}</b> aktif maç tarandı.",
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


def run_cli_demo(formatter: BettingBotFormatter, elite_only: bool = False, team: Optional[str] = None) -> None:
    if team:
        print(strip_html(formatter.format_match_xray(team)))
        return
    print(strip_html(formatter.format_trinity_system(hours=4, elite_only=elite_only)))


def main() -> None:
    parser = argparse.ArgumentParser(description="4 Saatlik 3'lü Sniper Kupon Botu (200₺ 5x | 100₺ 10x | 50₺ 20x)")
    parser.add_argument("--demo", action="store_true", help="Terminal üzerinde 4 saatlik 3'lü kupon sistemini çalıştırır")
    parser.add_argument("--analig", action="store_true", help="Sadece Ana Ligler (Lig 1) için 4 saatlik 3'lü kupon sistemini gösterir")
    parser.add_argument("--analiz", type=str, help="Belirli bir takımın maçını analiz eder")
    parser.add_argument("--token", type=str, help="Telegram Bot Token")
    args = parser.parse_args()

    fetcher = GlobalMatchDataFetcher()
    analyzer = MatchQuantitativeAnalyzer()
    formatter = BettingBotFormatter(fetcher, analyzer)

    if args.demo or args.analig or args.analiz:
        run_cli_demo(formatter, elite_only=args.analig, team=args.analiz)
        return

    token = args.token or load_bot_token()
    if not token:
        print("⚠️ Telegram Bot Token bulunamadı. Canlı Terminal 3'lü Kupon Demo Modu çalıştırılıyor...\n")
        run_cli_demo(formatter)
        return

    bot = TelegramBettingBot(token, formatter)
    bot.run_polling()


if __name__ == "__main__":
    main()
