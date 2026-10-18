#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Global Football Data & Odds Fetcher
Ücretsiz Kaynaklar:
1. Nesine / İddaa Global Bülten API (1300+ maç, 50+ bahis marketi ve anlık oranlar)
2. ESPN Global Soccer API (Takım son 5 maç formu, H2H karşılaşma geçmişi, lig istatistikleri)
3. Mackolik Canlı & Arşiv Veri API (Canlı skor ve maç durumu zenginleştirmesi)
"""

import gzip
import json
import os
import re
import time
import urllib.request
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple


CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")
os.makedirs(CACHE_DIR, exist_ok=True)


def _normalize_team_name(name: str) -> str:
    """Takım isimlerini kaynaklar arası eşleştirmek için normalize eder."""
    if not name:
        return ""
    tr_map = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")
    raw_clean = re.sub(r"[^a-z0-9]", "", name.translate(tr_map).lower())
    s = name.translate(tr_map).lower()
    s = re.sub(r"\b(fc|fk|sc|sk|ac|as|cd|cf|ud|sd|afc|bk|ik|if|united|city|club|deportivo|real|sporting|al)\b", "", s)
    s = re.sub(r"[^a-z0-9]", "", s).strip()
    return s if s else raw_clean


class GlobalMatchDataFetcher:
    """
    Nesine Global Bülten, ESPN Soccer ve Mackolik kaynaklarından
    ücretsiz olarak maç, oran, son 5 maç formu ve H2H verilerini çeker.
    """

    NESINE_BULLETIN_URL = "https://cdnbulten.nesine.com/api/bulten/getprebultenfull"
    ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/all/scoreboard"
    ESPN_SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/all/summary?event={event_id}"
    MACKOLIK_LIVE_URL = "https://vd.mackolik.com/livedata?group=0"

    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Encoding": "gzip, deflate",
    }

    def __init__(self, cache_ttl_seconds: int = 600):
        self.cache_ttl = cache_ttl_seconds
        self._espn_index: Dict[str, Dict[str, Any]] = {}
        self._espn_summary_cache: Dict[str, Dict[str, Any]] = {}

    def _http_get_json(self, url: str, timeout: int = 10) -> Any:
        req = urllib.request.Request(url, headers=self.HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if resp.info().get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return json.loads(raw.decode("utf-8", errors="replace"))

    def _get_cached_or_fetch(self, cache_key: str, url: str, ttl: Optional[int] = None) -> Any:
        ttl = ttl if ttl is not None else self.cache_ttl
        cache_path = os.path.join(CACHE_DIR, f"{cache_key}.json")
        if os.path.exists(cache_path):
            age = time.time() - os.path.getmtime(cache_path)
            if age < ttl:
                try:
                    with open(cache_path, "r", encoding="utf-8") as f:
                        return json.load(f)
                except Exception:
                    pass
        data = self._http_get_json(url)
        try:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception:
            pass
        return data

    def _load_espn_scoreboard(self) -> None:
        """ESPN global futbol bültenini çekip takım bazlı indeksler."""
        if self._espn_index:
            return
        try:
            data = self._get_cached_or_fetch("espn_scoreboard", self.ESPN_SCOREBOARD_URL, ttl=900)
            for ev in data.get("events", []):
                ev_id = str(ev.get("id", ""))
                comps = ev.get("competitions", [])
                if not comps:
                    continue
                comp = comps[0]
                competitors = comp.get("competitors", [])
                if len(competitors) < 2:
                    continue
                home_c = next((c for c in competitors if c.get("homeAway") == "home"), competitors[0])
                away_c = next((c for c in competitors if c.get("homeAway") == "away"), competitors[1])

                h_name = home_c.get("team", {}).get("displayName", "")
                a_name = away_c.get("team", {}).get("displayName", "")
                h_form = home_c.get("form", "")
                a_form = away_c.get("form", "")
                h_rec = (home_c.get("records") or [{}])[0].get("summary", "")
                a_rec = (away_c.get("records") or [{}])[0].get("summary", "")

                info = {
                    "event_id": ev_id,
                    "home_name": h_name,
                    "away_name": a_name,
                    "home_form": h_form,
                    "away_form": a_form,
                    "home_record": h_rec,
                    "away_record": a_rec,
                }
                h_norm = _normalize_team_name(h_name)
                a_norm = _normalize_team_name(a_name)
                if h_norm and a_norm:
                    self._espn_index[f"{h_norm}__{a_norm}"] = info
                    self._espn_index[h_norm] = info
                    self._espn_index[a_norm] = info
        except Exception:
            pass

    def fetch_espn_match_details(self, event_id: str) -> Dict[str, Any]:
        """ESPN Summary API'den son 5 maç (lastFiveGames) ve H2H (seasonseries) çeker."""
        if event_id in self._espn_summary_cache:
            return self._espn_summary_cache[event_id]

        result: Dict[str, Any] = {
            "home_last5_goals_for": None,
            "home_last5_goals_against": None,
            "home_last5_btts_rate": None,
            "home_last5_over25_rate": None,
            "away_last5_goals_for": None,
            "away_last5_goals_against": None,
            "away_last5_btts_rate": None,
            "away_last5_over25_rate": None,
            "h2h_btts_rate": None,
            "h2h_over25_rate": None,
            "h2h_avg_goals": None,
            "home_scores_str": [],
            "away_scores_str": [],
        }
        try:
            url = self.ESPN_SUMMARY_URL.format(event_id=event_id)
            data = self._get_cached_or_fetch(f"espn_sum_{event_id}", url, ttl=1800)

            lfg = data.get("lastFiveGames", [])
            if len(lfg) >= 2:
                for idx, prefix in [(0, "home"), (1, "away")]:
                    team_id = str(lfg[idx].get("team", {}).get("id", ""))
                    evs = lfg[idx].get("events", [])
                    gf_list, ga_list, btts_cnt, o25_cnt, scores_str = [], [], 0, 0, []
                    for e in evs[:5]:
                        try:
                            hs = int(e.get("homeTeamScore", 0))
                            as_ = int(e.get("awayTeamScore", 0))
                            if str(e.get("homeTeamId", "")) == team_id:
                                gf, ga = hs, as_
                            else:
                                gf, ga = as_, hs
                            gf_list.append(gf)
                            ga_list.append(ga)
                            if gf >= 1 and ga >= 1:
                                btts_cnt += 1
                            if (gf + ga) >= 3:
                                o25_cnt += 1
                            scores_str.append(f"{gf}-{ga}")
                        except Exception:
                            continue
                    if gf_list:
                        n = len(gf_list)
                        result[f"{prefix}_last5_goals_for"] = round(sum(gf_list) / n, 2)
                        result[f"{prefix}_last5_goals_against"] = round(sum(ga_list) / n, 2)
                        result[f"{prefix}_last5_btts_rate"] = round(btts_cnt / n, 2)
                        result[f"{prefix}_last5_over25_rate"] = round(o25_cnt / n, 2)
                        result[f"{prefix}_scores_str"] = scores_str

            # H2H (seasonseries)
            series = data.get("seasonseries", [])
            if series and series[0].get("events"):
                h2h_evs = series[0]["events"]
                tot_g, btts_c, o25_c, cnt = 0, 0, 0, 0
                for e in h2h_evs[:6]:
                    comps = e.get("competitors", [])
                    if len(comps) >= 2:
                        try:
                            s1 = int(comps[0].get("score", 0))
                            s2 = int(comps[1].get("score", 0))
                            tot_g += (s1 + s2)
                            if s1 >= 1 and s2 >= 1:
                                btts_c += 1
                            if (s1 + s2) >= 3:
                                o25_c += 1
                            cnt += 1
                        except Exception:
                            continue
                if cnt > 0:
                    result["h2h_avg_goals"] = round(tot_g / cnt, 2)
                    result["h2h_btts_rate"] = round(btts_c / cnt, 2)
                    result["h2h_over25_rate"] = round(o25_c / cnt, 2)
        except Exception:
            pass

        self._espn_summary_cache[event_id] = result
        return result

    @staticmethod
    def _parse_nesine_markets(ma_list: List[Dict[str, Any]]) -> Dict[str, float]:
        """
        Nesine bültenindeki MA (Market Array) içinden stratejiler için gereken
        tüm bahis market oranlarını ayrıştırır.
        """
        odds: Dict[str, float] = {}

        def _get_odd(m: Dict[str, Any], outcome_no: int) -> Optional[float]:
            for o in m.get("OCA", []):
                if o.get("N") == outcome_no:
                    val = o.get("O")
                    if isinstance(val, (int, float)) and val > 1.0:
                        return float(val)
            return None

        for m in ma_list:
            mtid = m.get("MTID")
            sov = float(m.get("SOV") or 0.0)

            # 1: Maç Sonucu (MS 1 / X / 2)
            if mtid == 1:
                odds["ms_1"] = _get_odd(m, 1)
                odds["ms_x"] = _get_odd(m, 2)
                odds["ms_2"] = _get_odd(m, 3)

            # 3: Çifte Şans (1-X, 1-2, X-2)
            elif mtid == 3:
                odds["cs_1x"] = _get_odd(m, 1)
                odds["cs_12"] = _get_odd(m, 2)
                odds["cs_x2"] = _get_odd(m, 3)

            # 7: İlk Yarı Sonucu (İY 1 / X / 2)
            elif mtid == 7:
                odds["iy_1"] = _get_odd(m, 1)
                odds["iy_x"] = _get_odd(m, 2)
                odds["iy_2"] = _get_odd(m, 3)

            # 9: İkinci Yarı Sonucu
            elif mtid == 9:
                odds["ikinci_yari_1"] = _get_odd(m, 1)
                odds["ikinci_yari_x"] = _get_odd(m, 2)
                odds["ikinci_yari_2"] = _get_odd(m, 3)

            # 11: 1.5 Alt / Üst
            elif mtid == 11 and abs(sov - 1.5) < 0.1:
                odds["alt_15"] = _get_odd(m, 1)
                odds["ust_15"] = _get_odd(m, 2)

            # 12: 2.5 Alt / Üst
            elif mtid == 12 and abs(sov - 2.5) < 0.1:
                odds["alt_25"] = _get_odd(m, 1)
                odds["ust_25"] = _get_odd(m, 2)

            # 13: 3.5 Alt / Üst
            elif mtid == 13 and abs(sov - 3.5) < 0.1:
                odds["alt_35"] = _get_odd(m, 1)
                odds["ust_35"] = _get_odd(m, 2)

            # 155: 4.5 ve 5.5 Alt / Üst
            elif mtid == 155:
                if abs(sov - 4.5) < 0.1:
                    odds["alt_45"] = _get_odd(m, 1)
                    odds["ust_45"] = _get_odd(m, 2)
                elif abs(sov - 5.5) < 0.1:
                    odds["alt_55"] = _get_odd(m, 1)
                    odds["ust_55"] = _get_odd(m, 2)

            # 209: İlk Yarı 0.5 Alt / Üst
            elif mtid == 209 and abs(sov - 0.5) < 0.1:
                odds["iy_alt_05"] = _get_odd(m, 1)
                odds["iy_ust_05"] = _get_odd(m, 2)

            # 14: İlk Yarı 1.5 Alt / Üst
            elif mtid == 14 and abs(sov - 1.5) < 0.1:
                odds["iy_alt_15"] = _get_odd(m, 1)
                odds["iy_ust_15"] = _get_odd(m, 2)

            # 15: İlk Yarı 2.5 Alt / Üst
            elif mtid == 15 and abs(sov - 2.5) < 0.1:
                odds["iy_alt_25"] = _get_odd(m, 1)
                odds["iy_ust_25"] = _get_odd(m, 2)

            # 20: Ev Sahibi 1.5 Alt / Üst
            elif mtid == 20 and abs(sov - 1.5) < 0.1:
                odds["ev_alt_15"] = _get_odd(m, 1)
                odds["ev_ust_15"] = _get_odd(m, 2)

            # 29: Deplasman 1.5 Alt / Üst
            elif mtid == 29 and abs(sov - 1.5) < 0.1:
                odds["dep_alt_15"] = _get_odd(m, 1)
                odds["dep_ust_15"] = _get_odd(m, 2)

            # 326: Ev Sahibi 2.5 Alt / Üst
            elif mtid == 326 and abs(sov - 2.5) < 0.1:
                odds["ev_ust_25"] = _get_odd(m, 2)

            # 328: Deplasman 2.5 Alt / Üst
            elif mtid == 328 and abs(sov - 2.5) < 0.1:
                odds["dep_ust_25"] = _get_odd(m, 2)

            # 38: Karşılıklı Gol (KG Var / KG Yok)
            elif mtid == 38:
                odds["kg_var"] = _get_odd(m, 1)
                odds["kg_yok"] = _get_odd(m, 2)

            # 43: Toplam Gol Aralığı (0-1, 2-3, 4-5, 6+ Gol)
            elif mtid == 43:
                odds["tg_0_1"] = _get_odd(m, 1)
                odds["tg_2_3"] = _get_odd(m, 2)
                odds["tg_4_5"] = _get_odd(m, 3)
                odds["arti_6_gol"] = _get_odd(m, 4)

            # 295: İlk Yarı KG Var / Yok
            elif mtid == 295:
                odds["iy_kg_var"] = _get_odd(m, 1)
                odds["iy_kg_yok"] = _get_odd(m, 2)

            # 296: İkinci Yarı KG Var / Yok
            elif mtid == 296:
                odds["ikinci_yari_kg_var"] = _get_odd(m, 1)
                odds["ikinci_yari_kg_yok"] = _get_odd(m, 2)

            # 591: Her İki Yarıda da Karşılıklı Gol (İki Yarı KG Evet/Evet)
            elif mtid == 591:
                odds["iki_yari_kg_var"] = _get_odd(m, 1)

            # 450: Her İki Yarıda Gol Olur Mu
            elif mtid == 450:
                odds["iki_yari_gol_var"] = _get_odd(m, 1)
                odds["iki_yari_gol_yok"] = _get_odd(m, 2)

            # 529: İki Yarı da 1.5 Üst
            elif mtid == 529:
                odds["iki_yari_15_ust"] = _get_odd(m, 1)

            # 343: MS ve 2.5 Alt/Üst (4=MS1&Üst, 5=MSX&Üst, 6=MS2&Üst)
            elif mtid == 343 and abs(sov - 2.5) < 0.1:
                odds["ms1_ve_25_ust"] = _get_odd(m, 4)
                odds["ms2_ve_25_ust"] = _get_odd(m, 6)

            # 446: 2.5 Alt/Üst ve KG (2=2.5 Üst & KG Var)
            elif mtid == 446 and abs(sov - 2.5) < 0.1:
                odds["kg_var_ve_25_ust"] = _get_odd(m, 2)

            # 414: MS ve KG Var (1=MS1&KG, 3=MSX&KG, 5=MS2&KG)
            elif mtid == 414:
                odds["ms1_ve_kg_var"] = _get_odd(m, 1)
                odds["ms2_ve_kg_var"] = _get_odd(m, 5)

            # 216: Toplam Korner Alt/Üst (8.5, 9.5, 10.5)
            elif mtid == 216:
                if abs(sov - 8.5) < 0.1:
                    odds["korner_85_ust"] = _get_odd(m, 2)
                elif abs(sov - 9.5) < 0.1:
                    odds["korner_95_alt"] = _get_odd(m, 1)
                    odds["korner_95_ust"] = _get_odd(m, 2)
                elif abs(sov - 10.5) < 0.1:
                    odds["korner_105_ust"] = _get_odd(m, 2)

            # NOT: MTID 455 / 457 (takım iki yarıda gol) ve 225 (kırmızı kart) eşleşmeleri
            # oran tutarlılık testinden geçemedi; yanlış bahis önermemek için devre dışı.

            # 301: Toplam Kart Puanı / Kart Alt/Üst
            elif mtid == 301:
                if abs(sov - 3.5) < 0.1:
                    odds["kart_35_alt"] = _get_odd(m, 1)
                    odds["kart_35_ust"] = _get_odd(m, 2)
                elif abs(sov - 4.5) < 0.1:
                    odds["kart_45_alt"] = _get_odd(m, 1)
                    odds["kart_45_ust"] = _get_odd(m, 2)
                elif abs(sov - 2.5) < 0.1:
                    odds["kart_25_alt"] = _get_odd(m, 1)
                    odds["kart_25_ust"] = _get_odd(m, 2)

            # 218: İlk Yarı Korner Alt/Üst
            elif mtid == 218 and abs(sov - 4.5) < 0.1:
                odds["iy_korner_45_alt"] = _get_odd(m, 1)
                odds["iy_korner_45_ust"] = _get_odd(m, 2)

            # 5: İY / MS (1/2 ve 2/1 sürprizleri dahil)
            elif mtid == 5:
                odds["iyms_1_1"] = _get_odd(m, 1)
                odds["iyms_1_x"] = _get_odd(m, 2)
                odds["iyms_1_2"] = _get_odd(m, 3)
                odds["iyms_x_1"] = _get_odd(m, 4)
                odds["iyms_x_x"] = _get_odd(m, 5)
                odds["iyms_x_2"] = _get_odd(m, 6)
                odds["iyms_2_1"] = _get_odd(m, 7)
                odds["iyms_2_x"] = _get_odd(m, 8)
                odds["iyms_2_2"] = _get_odd(m, 9)

            # 777: Maç Skoru (Kesin Skor)
            elif mtid == 777:
                for o in m.get("OCA", []):
                    on = o.get("ON", "").strip()
                    oval = o.get("O")
                    if not (isinstance(oval, (int, float)) and oval > 1.0):
                        continue
                    if ":" in on:
                        h_s, a_s = on.split(":", 1)
                        odds[f"skor_{h_s}_{a_s}"] = float(oval)
                    elif on.lower() == "diğer":
                        odds["skor_diger"] = float(oval)

        return {k: v for k, v in odds.items() if v is not None}

    def fetch_matches(self, only_today_and_tomorrow: bool = False, max_matches: int = 600) -> List[Dict[str, Any]]:
        """
        Global bültendeki futbol maçlarını, lig bilgilerini, oranlarını ve
        varsa ESPN/Mackolik takım form verilerini birleştirerek döndürür.
        """
        raw_data = self._get_cached_or_fetch("nesine_bulten", self.NESINE_BULLETIN_URL, ttl=self.cache_ttl)
        self._load_espn_scoreboard()

        sg = raw_data.get("sg", {})
        leagues_raw = sg.get("LA", [])
        league_map: Dict[int, Dict[str, Any]] = {}
        for lg in leagues_raw:
            lid = lg.get("LID")
            if lid is not None:
                league_map[lid] = {
                    "name": lg.get("N", "Bilinmeyen Lig"),
                    "country": lg.get("CC", ""),
                    "order": lg.get("SO", 9999),
                }

        events = sg.get("EA", [])
        now = datetime.now()
        matches: List[Dict[str, Any]] = []

        for ev in events:
            # Sadece Futbol (GT == 1 veya TYPE == 1) ve takım isimleri olanlar
            if ev.get("GT") != 1 or not ev.get("HN") or not ev.get("AN"):
                continue

            ma_list = ev.get("MA", [])
            if len(ma_list) < 5:
                continue

            odds = self._parse_nesine_markets(ma_list)
            # Temel marketleri olmayan maçları atla
            if "ms_1" not in odds or ("ust_25" not in odds and "kg_var" not in odds):
                continue

            lid = ev.get("LID") or ev.get("LC")
            lg_info = league_map.get(lid, {"name": f"Lig #{lid}", "country": "", "order": 9999})
            date_str = ev.get("D", "")
            time_str = ev.get("T", "")

            try:
                start_dt = datetime.strptime(f"{date_str} {time_str}", "%d.%m.%Y %H:%M")
            except ValueError:
                continue
            # Başlamış / bahsi kapanmış maçları alma
            if start_dt <= now:
                continue
            if only_today_and_tomorrow and (start_dt - now).days >= 2:
                continue

            # MBS (minimum bahis sayısı) MS marketinden okunur
            ms_market = next((mk for mk in ma_list if mk.get("MTID") == 1), {})
            mbs_val = ms_market.get("MBS", 1)

            home_name = ev["HN"].strip()
            away_name = ev["AN"].strip()

            # ESPN eşleşmesi kontrolü
            h_norm = _normalize_team_name(home_name)
            a_norm = _normalize_team_name(away_name)
            espn_info = (
                self._espn_index.get(f"{h_norm}__{a_norm}")
                or self._espn_index.get(h_norm)
                or self._espn_index.get(a_norm)
            )

            match_obj: Dict[str, Any] = {
                "code": str(ev.get("C", "")),
                "event_id": str(ev.get("EV", "")),
                "home": home_name,
                "away": away_name,
                "date": date_str,
                "time": time_str,
                "league": lg_info["name"],
                "country": lg_info["country"],
                "league_order": lg_info["order"],
                "mbs": mbs_val,
                "start_dt": start_dt,
                "odds": odds,
                "espn": espn_info,
            }
            matches.append(match_obj)

        # En yakın başlayan maçlar önce (gerçek tarih-saate göre)
        matches.sort(key=lambda x: (x["start_dt"], x["league_order"]))
        self._update_odds_history(matches)
        return matches[:max_matches]

    ODDS_HISTORY_PATH = os.path.join(CACHE_DIR, "odds_history.json")

    def _update_odds_history(self, matches: List[Dict[str, Any]]) -> None:
        """Her bülten çekilişinde oranların anlık değişimini kaydeder."""
        history: Dict[str, Any] = {}
        if os.path.exists(self.ODDS_HISTORY_PATH):
            try:
                with open(self.ODDS_HISTORY_PATH, "r", encoding="utf-8") as f:
                    history = json.load(f)
            except Exception:
                history = {}

        now_ts = time.time()
        for m in matches:
            code = m["code"]
            curr_odds = m["odds"]
            if not code or not curr_odds:
                continue

            if code not in history:
                history[code] = {
                    "first_seen": now_ts,
                    "home": m["home"],
                    "away": m["away"],
                    "league": m["league"],
                    "time": f"{m['date']} {m['time']}",
                    "initial_odds": curr_odds,
                    "latest_odds": curr_odds,
                }
            else:
                history[code]["latest_odds"] = curr_odds

        # 1000 maçtan fazlasını saklayıp şişirmemek için temizle
        if len(history) > 1000:
            keys_to_keep = list(history.keys())[-750:]
            history = {k: history[k] for k in keys_to_keep}

        try:
            with open(self.ODDS_HISTORY_PATH, "w", encoding="utf-8") as f:
                json.dump(history, f, ensure_ascii=False)
        except Exception:
            pass

    def get_dropping_odds(self, min_drop_pct: float = 0.08) -> List[Dict[str, Any]]:
        """
        Bülten açılışından itibaren en az min_drop_pct (%8+) oran düşüşü yaşayan
        ve piyasa / akıllı para (Smart Money) ilgisi çeken maçları bulur.
        """
        if not os.path.exists(self.ODDS_HISTORY_PATH):
            return []

        try:
            with open(self.ODDS_HISTORY_PATH, "r", encoding="utf-8") as f:
                history = json.load(f)
        except Exception:
            return []

        drops = []
        market_labels = {
            "ms_1": "MS 1 (Ev Sahibi)",
            "ms_2": "MS 2 (Deplasman)",
            "ms_x": "MS X (Beraberlik)",
            "ust_25": "2.5 Gol Üst",
            "alt_25": "2.5 Gol Alt",
            "kg_var": "KG Var",
            "kg_yok": "KG Yok",
            "iy_ust_15": "İlk Yarı 1.5 Üst",
        }

        for code, entry in history.items():
            init_odds = entry.get("initial_odds", {})
            latest_odds = entry.get("latest_odds", {})
            for m_key, label in market_labels.items():
                i_val = init_odds.get(m_key)
                l_val = latest_odds.get(m_key)
                if i_val and l_val and isinstance(i_val, (int, float)) and isinstance(l_val, (int, float)):
                    if i_val > 1.25 and l_val < i_val:
                        drop_ratio = (i_val - l_val) / i_val
                        if drop_ratio >= min_drop_pct:
                            drops.append({
                                "code": code,
                                "match": f"{entry['home']} - {entry['away']}",
                                "league": entry.get("league", ""),
                                "time": entry.get("time", ""),
                                "market_key": m_key,
                                "market_name": label,
                                "initial_odd": round(i_val, 2),
                                "current_odd": round(l_val, 2),
                                "drop_pct": round(drop_ratio * 100, 1),
                            })

        drops.sort(key=lambda x: x["drop_pct"], reverse=True)
        return drops

    def fetch_mackolik_scores(self) -> Dict[str, Any]:
        """
        Mackolik canlı skor API'sinden anlık devam eden ve
        bitmiş maçları ayrıştırarak skor ve durumlarıyla döndürür.
        """
        live_matches: List[Dict[str, Any]] = []
        finished_matches: Dict[str, Dict[str, Any]] = {}

        try:
            raw = self._http_get_json(self.MACKOLIK_LIVE_URL, timeout=8)
            m_list = raw.get("m", [])
            for it in m_list:
                if len(it) < 8:
                    continue
                m_id = it[0]
                h_name = str(it[2]).strip()
                a_name = str(it[4]).strip()
                status = str(it[6]).strip()
                score = str(it[7]).strip()
                lg = ""
                if len(it) > 35 and isinstance(it[35], list) and len(it[35]) > 3:
                    lg = str(it[35][3]).strip()

                h_goals, a_goals = None, None
                for sep in ("-", ":"):
                    if sep in score:
                        parts = score.split(sep, 1)
                        try:
                            h_goals = int(parts[0].strip())
                            a_goals = int(parts[1].strip())
                        except ValueError:
                            pass
                        break

                norm_key = f"{_normalize_team_name(h_name)}__{_normalize_team_name(a_name)}"
                item_dict = {
                    "id": m_id,
                    "home": h_name,
                    "away": a_name,
                    "status": status,
                    "score": score,
                    "home_goals": h_goals,
                    "away_goals": a_goals,
                    "league": lg,
                }

                if status == "MS":
                    finished_matches[norm_key] = item_dict
                    finished_matches[_normalize_team_name(h_name)] = item_dict
                    finished_matches[_normalize_team_name(a_name)] = item_dict
                elif any(char.isdigit() for char in status) or status in ("İY", "Devre"):
                    live_matches.append(item_dict)
        except Exception:
            pass

        return {
            "live": live_matches,
            "finished": finished_matches,
        }


if __name__ == "__main__":
    fetcher = GlobalMatchDataFetcher()
    ms = fetcher.fetch_matches(max_matches=50)
    print(f"Toplam analiz edilebilir zengin futbol maçı: {len(ms)}")
    if ms:
        m0 = ms[0]
        print(f"Örnek Maç: {m0['home']} vs {m0['away']} ({m0['league']}) - {m0['date']} {m0['time']}")
        print(f"Çekilen Market Sayısı: {len(m0['odds'])}, Örnek Oranlar: {list(m0['odds'].items())[:10]}")
