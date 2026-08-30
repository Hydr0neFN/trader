[English](README.md) · **繁體中文**

# trader

適用於美股的多 LLM 演算法**模擬交易（paper-trading）**系統。於開盤時間按 cron 排程執行，透過語言模型集成（ensemble）分析約 50 檔 S&P 500 大型股標的，並透過 Alpaca 進行模擬下單。

> **僅限模擬交易。** 所有交易均使用 Alpaca 的模擬交易端點（`paper=True`）。不承擔任何真實資金風險。亦不對獲利能力作任何主張 — 此為研究鷹架。

## 運作原理

每次執行（平日 9:30–16:00 ET，每 30 分鐘一次）針對每批股票代碼執行以下流程：

1. **市場數據 + 新聞** — 透過 yfinance 取得價格歷史，透過 Alpaca 新聞 API 取得新聞標題。
2. **分析師**（Gemini）— 附帶信心度與理由的 BUY/SELL/HOLD 建議。沿著模型優先順序鏈（`gemini-3.6-flash` → `gemini-3.7-flash` → … → `gemini-3.1-flash-lite`）依序嘗試，以便在某個模型遇到額度限制時能平穩降級。
3. **情緒分析**（Hugging Face）— BULLISH/BEARISH/NEUTRAL 的第二意見。僅在與分析師**直接衝突**時（BUY 對上 BEARISH，或 SELL 對上 BULLISH）才會阻擋交易；NEUTRAL（無重大新聞／空白新聞）不會行使否決權。
4. **風險控管**（Claude）— 最終關卡；否決不安全的交易。在關鍵的出場決策上透過 Claude Agent SDK 使用 **Sonnet** — 運用您 **Claude Pro 方案內含額度** — 並使用 **Haiku** API 進行大批量的買入篩選；未設定訂閱 token 時會退回使用 Haiku。
5. **執行** — Alpaca 模擬訂單；強制停損底線**與保護利潤的移動停損**皆獨立於 LLM 強制執行。
6. **出場分析** — 未平倉部位由獨立的 Gemini **API** 出場分析師 + Claude 出場風險關卡重新評估。（Google 於 2026-06-18 停用了個人層級的 `gemini-cli`；該路徑預設關閉 — 僅在具備付費金鑰支援的 CLI 時才設定 `USE_GEMINI_EXIT_CLI=1`。）

提示詞規則禁止分析師在未提供新聞標題時捏造新聞 — 必須如實陳述資料缺失，而非產生幻覺。

## 安全防護機制（獨立於 LLM）

| 防護機制 | 預設值 | 環境變數覆寫 |
|------|---------|--------------|
| 強制停損 | 5% | `STOP_LOSS_PCT` |
| 移動停損（自高點回檔） | 3.5% | `TRAIL_STOP_PCT` |
| 移動停損啟動條件（啟動前所需漲幅） | 3% | `TRAIL_ACTIVATE_PCT` |
| 倉位規模 | 淨值的 2% | `POSITION_SIZE_PCT` |
| 最大同時持倉數 | 8 | `MAX_POSITIONS` |
| 每批股票代碼數 | 10 | `TICKER_BATCH_SIZE` |

**移動停損**僅在倉位的即時高點漲幅超越進場價 `TRAIL_ACTIVATE_PCT` 之後才會啟動，隨後在自該高點回檔 `TRAIL_STOP_PCT` 時出場 — 在獲利標的上鎖定收益，同時保留強制停損底線來管理從未上漲的標的。高點紀錄會持久化儲存在 `trade_logs/position_peaks.json` 中，並在每次執行時進行取樣。

## 安裝設定

```bash
pip install -r requirements.txt
cp .env.example ~/.env        # fill in your keys
```

`~/.env` 中所需金鑰：`ALPACA_API_KEY`、`ALPACA_SECRET_KEY`、`GEMINI_API_KEY`、`ANTHROPIC_API_KEY`、`HF_API_TOKEN`。詳見 `.env.example`。

**選用 — 透過訂閱使用 Claude Sonnet。** 若要透過 Claude Agent SDK 在 Claude **Sonnet** 上執行風險／出場關卡 — 運用您的 **Claude Pro 方案內含額度**而非按用量計費的 Haiku API token — 請新增 `CLAUDE_CODE_OAUTH_TOKEN`（來自 `claude setup-token`）。可微調參數：`CLAUDE_SDK_FOR`（`exits` [預設] | `all` | `none`）與 `CLAUDE_SDK_MODEL`（預設 `sonnet`）。若無 token，機器人將如以往般僅以 Haiku 執行。

## 執行

```bash
python3 trader.py
```

此腳本會自行限制在 9:30–16:00 ET 開盤時間內（並透過 Alpaca 的 clock 端點跳過休假日），因此在市場休市時會提早結束。

### Cron（開盤時間內每 30 分鐘一次）

```cron
*/30 9-15 * * 1-5 flock -n /tmp/trader.lock /usr/bin/python3 /path/to/trader.py >> /path/to/trade_logs/cron.log 2>&1
```

`flock -n` 可避免在前一次執行較慢時發生重疊堆積。請根據您伺服器的時區調整小時範圍 — 機器人無論如何都會自行強制遵循 ET 時間窗口。

## 儀表板

位於 `dashboard/` 的小型 Flask 應用程式，用於顯示倉位、決策與歷史紀錄。總覽頁面繪製投資組合價值隨時間變化的圖表，並帶有**歷史高點線與回撤陰影**。

```bash
python3 dashboard/app.py
```

## 專案結構

```
trader.py              # main pipeline (data → analyst → sentiment → risk → execute → exit)
requirements.txt
dashboard/
  app.py               # Flask dashboard
  templates/           # overview, positions, decisions, history
.env.example           # credential template (real keys live in ~/.env, never committed)
```
