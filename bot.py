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
import sys
import threading
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Set

from data_fetcher import GlobalMatchDataFetcher
from strategy_engine import MatchQuantitativeAnalyzer


CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
SUBSCRIBERS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache", "subscribers.json")


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


class BettingBotFormatter:
    """Tahminleri, maç analizlerini ve kuponları Telegram/Terminal için şık formatta üretir."""

    def __init__(self, fetcher: GlobalMatchDataFetcher, analyzer: MatchQuantitativeAnalyzer):
        self.fetcher = fetcher
        self.analyzer = analyzer
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

    def format_strategy_picks(self, strategy_key: str, top_n: int = 6) -> str:
        meta = self.analyzer.STRATEGY_METADATA.get(strategy_key)
        if not meta:
            return "❌ Geçersiz strateji kodu."

        matches = self.get_matches()
        picks = self.analyzer.scan_bulletin(matches, strategy_key, top_n=top_n, fetcher=self.fetcher)

        lines = [
            f"<b>{meta['title']}</b>",
            f"<i>{meta['desc']}</i>",
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
                f"<b>{idx}. ⚽ {m['home']} - {m['away']}</b>\n"
                f"   🏆 {m['league']} | 🕒 {m['date']} {m['time']}\n"
                f"   🎯 <b>Tahmin:</b> {s['pick']}  |  💰 <b>Oran: {s['odd']:.2f}</b>\n"
                f"   📈 <b>Model Olasılığı:</b> %{s['prob']:.1f} | 🔥 <b>Güven:</b> %{s['confidence']:.1f} ({ev_str})\n"
                f"   🧮 <b>Beklenen Gol (xG):</b> Ev {a['lam_home']} - Dep {a['lam_away']} (Top: {a['lam_total']}) | 🎲 Skor: {top_sc}\n"
                f"   💡 <b>Neden?:</b> {'; '.join(s['reasons'])}"
            )
            lines.append("──────────────────────")

        return "\n".join(lines)

    def format_coupons(self) -> str:
        matches = self.get_matches()
        coupons = self.analyzer.build_smart_coupons(matches)
        lines = [
            "🎫 <b>GÜNÜN ALGORİTMİK HAZIR KUPONLARI</b>",
            f"<i>Dixon-Coles, Bivariate Poisson ve Çapraz Market (+EV) analizleriyle {len(matches)} maç taranarak oluşturuldu:</i>",
            "━━━━━━━━━━━━━━━━━━━━━━",
        ]

        for key in ["banko", "ideal", "surpriz"]:
            c = coupons[key]
            lines.append(f"<b>{c['title']}</b>")
            lines.append(f"<i>{c['desc']}</i>")
            if not c["picks"]:
                lines.append("   ⚠️ Uygun maç bulunamadı.\n")
                continue
            for idx, p in enumerate(c["picks"], 1):
                lines.append(
                    f"  {idx}) <b>{p['match']}</b> ({p['time']})\n"
                    f"     👉 <b>{p['pick']}</b> | Oran: <b>{p['odd']:.2f}</b> (Model: %{p['prob']:.1f})\n"
                    f"     📌 <i>{p['reason']}</i>"
                )
            lines.append(f"🔥 <b>Toplam Kupon Oranı: {c['total_odd']:.2f}</b>")
            lines.append("━━━━━━━━━━━━━━━━━━━━━━")

        return "\n".join(lines)

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
            {"text": "🎯 KG Var Taktiği", "callback_data": "strat:kg_var"},
            {"text": "🏆 MS Favori/Value", "callback_data": "strat:ms_banko"},
        ],
        [
            {"text": "🔥 2.5 Üst Taktiği", "callback_data": "strat:ust_25"},
            {"text": "🚀 3.5 Üst Fırtınası", "callback_data": "strat:ust_35"},
        ],
        [
            {"text": "💣 +6 Gol (Kaos)", "callback_data": "strat:arti_6_gol"},
            {"text": "⚡ İki Yarı KG (Evet/Evet)", "callback_data": "strat:iki_yari_kg"},
        ],
        [
            {"text": "⏱️ İlk Yarı 1.5 Üst", "callback_data": "strat:iy_ust"},
            {"text": "💎 Kombo (KG/MS & Üst)", "callback_data": "strat:kombo"},
        ],
        [
            {"text": "📐 Korner 9.5 Üst", "callback_data": "strat:korner"},
            {"text": "🧠 Sharp Value (+EV)", "callback_data": "strat:value_bet"},
        ],
        [
            {"text": "🎫 Günün Hazır Kuponları (Banko / İdeal / Sürpriz)", "callback_data": "action:coupons"},
        ],
        [
            {"text": "📚 Neye Göre Tahmin Veriyor? (Global Modeller)", "callback_data": "action:models"},
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

    def send_message(self, chat_id: int, text: str, show_menu: bool = True) -> None:
        # Telegram mesaj sınırı 4096 karakterdir; uzunsa bölerek gönder
        chunks = [text[i : i + 3900] for i in range(0, len(text), 3900)]
        for idx, chunk in enumerate(chunks):
            payload: Dict[str, Any] = {
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            if show_menu and idx == len(chunks) - 1:
                payload["reply_markup"] = {"inline_keyboard": self.STRATEGY_BUTTONS}
            try:
                self._api_call("sendMessage", payload)
            except Exception as e:
                print(f"[Telegram Send Error] {e}")

    def handle_command(self, chat_id: int, text: str) -> None:
        parts = text.strip().split(maxsplit=1)
        cmd = parts[0].lower().split("@")[0]
        arg = parts[1] if len(parts) > 1 else ""

        cmd_map = {
            "/kg": "kg_var",
            "/ms": "ms_banko",
            "/ust25": "ust_25",
            "/ust35": "ust_35",
            "/arti6": "arti_6_gol",
            "/ikiyarikg": "iki_yari_kg",
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
                "• <code>/kg</code> — 🎯 KG Var Taktiği\n"
                "• <code>/ms</code> — 🏆 MS Favori & Value Taktiği\n"
                "• <code>/ust25</code> — 🔥 2.5 Üst Taktiği\n"
                "• <code>/ust35</code> — 🚀 3.5 Üst Gol Fırtınası\n"
                "• <code>/arti6</code> — 💣 +6 Gol (Ultra Varyans) Taktiği\n"
                "• <code>/ikiyarikg</code> — ⚡ İki Yarıda da KG Var (Evet/Evet)\n"
                "• <code>/iyust</code> — ⏱️ İlk Yarı 1.5 Üst Taktiği\n"
                "• <code>/kombo</code> — 💎 KG/MS & 2.5 Üst Kombo\n"
                "• <code>/korner</code> — 📐 Korner 9.5 Üst Taktiği\n"
                "• <code>/value</code> — 🧠 Oran Sapması (+EV) Value Bet\n"
                "• <code>/kupon</code> — 🎫 Günün 3 Hazır Kuponu\n"
                "• <code>/analiz TakımAdı</code> — 🔬 Maç X-Ray Analizi (Örn: <code>/analiz Chelsea</code>)\n"
                "• <code>/oto_ac</code> / <code>/oto_kapat</code> — 🔔 Otomatik Fırsat Bildirimi"
            )
            self.send_message(chat_id, welcome, show_menu=True)

        elif cmd in cmd_map:
            reply = self.formatter.format_strategy_picks(cmd_map[cmd])
            self.send_message(chat_id, reply, show_menu=True)

        elif cmd == "/kupon":
            self.send_message(chat_id, self.formatter.format_coupons(), show_menu=True)

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
            {"command": "kupon", "description": "🎫 Günün Hazır Kuponları (Banko / İdeal / Sürpriz)"},
            {"command": "kg", "description": "🎯 KG Var (BTTS Sniper) Taktiği"},
            {"command": "ms", "description": "🏆 MS (Maç Sonucu) Favori & Value Taktiği"},
            {"command": "ust25", "description": "🔥 2.5 Üst Gol Taktiği"},
            {"command": "ust35", "description": "🚀 3.5 Üst Gol Fırtınası"},
            {"command": "arti6", "description": "💣 +6 Gol (Ultra Varyans / Kaos) Taktiği"},
            {"command": "ikiyarikg", "description": "⚡ İki Yarıda da KG Var (Evet/Evet) Taktiği"},
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
                            self._api_call("answerCallbackQuery", {"callback_query_id": cq_id})
                        except Exception:
                            pass

                        if data.startswith("strat:"):
                            s_key = data.split(":", 1)[1]
                            reply = self.formatter.format_strategy_picks(s_key)
                            self.send_message(chat_id, reply, show_menu=True)
                        elif data == "action:coupons":
                            self.send_message(chat_id, self.formatter.format_coupons(), show_menu=True)
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


def strip_html(text: str) -> str:
    """Terminal çıktısı için HTML etiketlerini temizler."""
    import re
    return re.sub(r"<[^>]+>", "", text)


def run_cli_demo(formatter: BettingBotFormatter, strategy: Optional[str] = None, team: Optional[str] = None) -> None:
    if team:
        print(strip_html(formatter.format_match_xray(team)))
        return

    if strategy:
        print(strip_html(formatter.format_strategy_picks(strategy, top_n=5)))
        return

    # Varsayılan kapsamlı demo raporu
    for skey in ["kg_var", "ms_banko", "ust_25", "arti_6_gol", "iki_yari_kg", "value_bet"]:
        print(strip_html(formatter.format_strategy_picks(skey, top_n=3)))
        print("\n")
    print(strip_html(formatter.format_coupons()))


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
