[English](README.md) · **繁體中文**

# trader

一套用多個 LLM 對美股做**模擬交易**的機器人。它照 cron 排程在開盤時間跑，分析約 50 檔 S&P 500 大型股，再透過 Alpaca 下模擬單。現在已經退役，這個儲存庫留下來當紀錄。

> **狀態：已於 2026-10-10 退役。** 機器人已經停止運作，本儲存庫僅作為紀錄保留。位於 trader.hydr0negnetwork.de 的即時儀表板已改為靜態的退役說明頁。模擬倉位是**凍結而非平倉**。姊妹專案 [DOWTrade](https://github.com/Hydr0neFN/DOWTrade) **並未**退役，仍在運行中。

> **僅限模擬交易。** 所有交易都走 Alpaca 的模擬端點（`paper=True`）。不會動到真錢。也沒有說它會賺錢 —— 這是一個研究用的鷹架。

## 總覽

| 項目 | 內容 |
|------|------|
| 問題 | 多個 LLM 組成的集成，跟買入持有 SPY 相比，在 120 個交易日裡實際會跑出什麼結果 |
| 做了什麼 | 一套 cron 驅動的模擬交易流程：行情與新聞、分析師集成、情緒檢查、Claude 風險關卡、Alpaca 模擬下單 |
| 結果 | 120 個交易日 +7.06%（$100,000 變成 $107,064.60），同期 SPY +12.68%，相差 -5.62 個百分點；alpha 的年化 95% 信賴區間從 -18.3% 到 +26.1% |
| 技術 | Python、Alpaca、yfinance、DeepSeek / agy / Gemini、Claude Agent SDK、Cloudflare Workers AI、Flask |
| 狀態 | 2026-10-10 退役；倉位凍結，沒有平倉 |

## 運作方式

每一輪把一批股票代碼送進同一條鏈：

```
cron: every 30 min, 9:30-16:00 ET, weekdays
  -> market data + news     yfinance, Alpaca news API
  -> analyst                DeepSeek -> agy -> Gemini API chain
  -> sentiment              DeepSeek (Hugging Face) -> Cloudflare Workers AI
  -> risk gate              Claude Sonnet (exits) / Haiku (buy screen)
  -> execution              Alpaca paper order, hard stop + trailing stop
  -> exit analysis          exit analyst + Claude exit-risk gate
```

- 分析師回傳 BUY、SELL 或 HOLD，附上信心度與理由。
- 情緒這一段只有在跟分析師直接衝突時才會擋單。
- 提示詞規則禁止分析師在沒有新聞標題時自己編新聞。
- 完整流程、模型名稱、後備順序與可調參數在 [docs/pipeline.zh-TW.md](docs/pipeline.zh-TW.md)。

## 安全防護機制（獨立於 LLM）

停損、倉位與現金上限都由程式碼執行，不交給 LLM。

<!-- RAILS:START -->
| 防護機制 | 預設值 | 環境變數覆寫 |
|------|---------|--------------|
| 強制停損 | 5% | `STOP_LOSS_PCT` |
| 移動停損（自高點回檔） | 3.5% | `TRAIL_STOP_PCT` |
| 移動停損啟動條件（啟動前所需漲幅） | 3% | `TRAIL_ACTIVATE_PCT` |
| 倉位規模 | 淨值的 2% | `POSITION_SIZE_PCT` |
| 最大同時持倉數 | 8 | `MAX_POSITIONS` |
| 永不動用的現金底線 | $500 | `CASH_RESERVE_USD` |
| 現金裁切後的最小下單金額 | $750 | `MIN_TRADE_USD` |
| 每批股票代碼數 | 10 | `TICKER_BATCH_SIZE` |
| 分析師信心度門檻 | 70% | —（寫死） |
<!-- RAILS:END -->

這張表由 `tools/readme_rails.py` 從 `trader.py` 產生；提交前先跑 `--check`（有漂移就 exit 1），要重新產生則用 `--write`。信心度門檻、現金防護與移動停損的運作方式在 [docs/safety-rails.zh-TW.md](docs/safety-rails.zh-TW.md)。

## 結果與退役

- **症狀。** 2026-04-14 到 2026-10-09 收盤，機器人報酬 +7.06%，同期 SPY +12.68%，一樣是 120 個交易日。
- **量測。** 日報酬迴歸（n=124）得到 beta 0.417、alpha 年化 +3.9%、t = 0.35。95% 信賴區間是年化 [-18.3%, +26.1%]，估計值與零無法區分。要在 t=2 證明 alpha，需要約 16 年。
- **原因。** 風險調整後也落後（夏普值 1.55 對 2.08）。LLM 這條路只有維護成本，沒有研究回報；另外有四種機制讓機器人偏向不作為（失敗時一律 HOLD、關卡採 fail-closed 等）。
- **處理。** 擁有者於 2026-10-10 決定退役。倉位凍結，沒有平倉，儲存庫留下來當紀錄。
- **仍未解。** 這個研究問題在可用的時間尺度上不會有答案。

完整結果表、資料說明與退役原因在 [docs/results.zh-TW.md](docs/results.zh-TW.md)。

## 建置與執行

*歷史紀錄：機器人已退役，不再排程執行。以下步驟記錄的是它當時的執行方式。*

```bash
pip install -r requirements.txt
cp .env.example ~/.env        # fill in your keys
python3 trader.py
```

腳本會自行限制在 9:30-16:00 ET 的開盤時間內，並透過 Alpaca 的 clock 端點跳過休假日，市場休市時就提早結束。金鑰、選用供應商與 cron／healthcheck 的設定在 [docs/operations.zh-TW.md](docs/operations.zh-TW.md)。

## 專案結構

```
trader.py              # main pipeline (data → analyst → sentiment → risk → execute → exit)
llm_cost_report.py     # read-only summary of trade_logs/llm_calls.jsonl
tools/readme_rails.py  # derives the safety-rail table from trader.py (--check / --write)
healthcheck.py         # run-freshness + degraded-LLM + DeepSeek-down check, notifies via ntfy / webhook / email
requirements.txt
dashboard/
  app.py               # Flask dashboard
  i18n.py              # zh-TW / EN string table (shared byte-for-byte with DOWTrade)
  static/theme.css
  templates/           # overview, decisions, history + _lang_switch, _positions_table
.env.example           # credential template (real keys live in ~/.env, never committed)
```

## 限制與未解項目

- 績效落後買入持有 SPY 5.6 個百分點，風險調整後也落後（夏普值 1.55 對 2.08）。
- 缺口：2026-06-30 到 2026-07-13 是舊 Pi 的中斷（7 個交易日，倉位維持不動），2026-08-26 則沒有執行。
- 2026-10-09 以降級狀態執行（agy 額度用完，退回 Gemini flash-lite）。排除該日後結果是 +6.27%，對照 SPY +12.01%，主要結論不變。
- 這是模擬帳戶，24 檔凍結的持倉仍會持續按市價重估。

## 授權

MIT 授權，詳見 [LICENSE](LICENSE)。
