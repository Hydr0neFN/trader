[English](README.md) · **繁體中文**

# trader

適用於美股的多 LLM 演算法**模擬交易（paper-trading）**系統。於開盤時間按 cron 排程執行，透過語言模型集成（ensemble）分析約 50 檔 S&P 500 大型股標的，並透過 Alpaca 進行模擬下單。

> **僅限模擬交易。** 所有交易均使用 Alpaca 的模擬交易端點（`paper=True`）。不承擔任何真實資金風險。亦不對獲利能力作任何主張 — 此為研究鷹架。

## 運作原理

每次執行（平日 9:30–16:00 ET，每 30 分鐘一次）針對每批股票代碼執行以下流程：

1. **市場資料 + 新聞** — 透過 yfinance 取得價格歷史，透過 Alpaca 新聞 API 取得新聞標題。
2. **分析師**（DeepSeek → agy → Gemini）— 附帶信心度與理由的 BUY/SELL/HOLD 建議。設定 `USE_DEEPSEEK=1` 時，會先透過本機反向代理詢問 DeepSeek V4.1 Flash（見下方*透過 deeperseeker 使用 DeepSeek*）；接著是 agy（`USE_AGY_GEMINI=1`，Gemini 訂閱額度）；最後才是 Gemini API，並沿著模型優先順序鏈（`gemini-3.8-flash` → `gemini-3.6-flash` → … → `gemini-3.1-flash-lite`）依序嘗試，以便在某個模型遇到額度限制時能平穩降級。每筆決策都會記下實際回答的模型：`ds:v4.1flash`、`agy:Gemini 3.8 Flash (High)`、`gemini-…`，整批失敗則為 `none`。
3. **情緒分析**（DeepSeek → Cloudflare Workers AI）— BULLISH/BEARISH/NEUTRAL 的第二意見。僅在與分析師**直接衝突**時（BUY 對上 BEARISH，或 SELL 對上 BULLISH）才會阻擋交易；NEUTRAL（無重大新聞／空白新聞）不會行使否決權。模型鏈為 Hugging Face 的 `deepseek-ai/DeepSeek-V4.1-Flash`，接著是 Cloudflare Workers AI 的 `@cf/mistralai/mistral-small-3.1-24b-instruct` 與 `@cf/meta/llama-4-scout-17b-16e-instruct`。兩個後備刻意架在**不同的計費管道**上：舊的模型鏈四個全在 Hugging Face，額度一旦耗盡，四個會同時回傳 402。Cloudflare 免費層每日 10,000 Neurons，約等於 900 次情緒呼叫，因此在 Hugging Face 額度見底時這一段仍然存活。把答案放進 `reasoning` 而讓 `content` 空白的模型在此無法使用，排除依據是實測，不是名氣。
4. **風險控管**（Claude）— 最終關卡；否決不安全的交易。在關鍵的出場決策上透過 Claude Agent SDK 使用 **Sonnet** — 運用您 **Claude Pro 方案內含額度** — 並使用 **Haiku** API 進行大批量的買入篩選；未設定訂閱 token 時會退回使用 Haiku。
5. **執行** — Alpaca 模擬訂單；強制停損底線**與保護利潤的移動停損**皆獨立於 LLM 強制執行。
6. **出場分析** — 未平倉部位由出場分析師與 Claude 出場風險關卡重新評估。出場分析師的順序與第 2 步相同：先 DeepSeek（`USE_DEEPSEEK=1`），再透過 Antigravity CLI 使用 Google AI **訂閱**額度（`USE_AGY_GEMINI=1`），最後是 Gemini API 鏈；每一段失敗都會退回下一段，兩個旗標都未設定時則直接使用 API 鏈。（Google 於 2026-06-18 停用了個人層級的 `gemini-cli`；該路徑預設關閉 — 僅在具備付費金鑰支援的 CLI 時才設定 `USE_GEMINI_EXIT_CLI=1`。）

   請留意這道關卡造成的不對稱：判定為 `HOLD` 時會在 Claude 關卡之前就返回，因此關卡能否決不當的出場，卻無法補救被漏掉的出場。

提示詞規則禁止分析師在未提供新聞標題時捏造新聞 — 必須如實陳述資料缺失，而非產生幻覺。

## 安全防護機制（獨立於 LLM）

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

上面那張表是產生出來的，不是手打的：`tools/readme_rails.py` 會用 `ast` 解析 `trader.py`，重寫 `RAILS` 標記之間的區塊。提交前先跑 `--check`（有漂移就 exit 1），要重新產生則用 `--write`。它讀 AST 而不是對原始碼做 grep，因為 regex 分不出被註解掉的呼叫、兩個預設值不同的呼叫會默默取第一個、而且遇到非字串字面值的預設值就整個放棄 —— 每一種情況都可能發布出一個程式根本沒在用的數字。同一個變數被讀兩次卻給出不同預設值，會被視為錯誤而非擲骰子。這個專案過去發布出去的每一個過期數字 —— GitHub 主頁寫死的 `/20`、儀表板頁尾手寫的模型清單 —— 都是某個被抄過去的值。抄寫本身就是 bug，推導才是解法。


信心度低於門檻的 BUY 或 SELL 會被否決並記下理由，與 Haiku 否決或情緒指標相牴觸的處理方式相同。這一條刻意不提供環境變數覆寫：針對本機器人自身歷史的量測顯示，信心度分數對結果沒有預測力，因此它只保留為一道粗略的門檻，而不會被提升為排序依據。

倉位規模是以*投資組合總值*的百分比計算，這個數字完全不反映已交割的現金。在加入**現金防護**之前，`MAX_POSITIONS x POSITION_SIZE_PCT <= 100%` 是唯一阻止機器人動用融資的機制 — 而模擬帳戶通常配有約 4 倍的購買力，因此超出預算的訂單不會失敗，而是靜靜地用借來的錢成交。`execute_trades()` 現在會在整輪執行中追蹤可動用現金，把訂單裁切到現金足以支應的數量，並在現金耗盡時記錄 `SKIPPED_CASH`。前一輪遺留的未成交買單會先從現金與持倉槽兩邊的預算扣除再重新分配，因為 Alpaca 是在成交而非送單時才扣現金。賣出會排在買入之前處理並把所得回補，讓滿倉狀態仍能換股；裁切後低於 `MIN_TRADE_USD` 的單會直接跳過，不讓零碎部位佔掉一個持倉槽。

**移動停損**僅在倉位的即時高點漲幅超越進場價 `TRAIL_ACTIVATE_PCT` 之後才會啟動，隨後在自該高點回檔 `TRAIL_STOP_PCT` 時出場 — 在獲利標的上鎖定收益，同時保留強制停損底線來管理從未上漲的標的。高點紀錄會持久化儲存在 `trade_logs/position_peaks.json` 中，並在每次執行時進行取樣。

## 安裝設定

```bash
pip install -r requirements.txt
cp .env.example ~/.env        # fill in your keys
```

`~/.env` 中所需金鑰：`ALPACA_API_KEY`、`ALPACA_SECRET_KEY`、`GEMINI_API_KEY`、`ANTHROPIC_API_KEY`、`HF_API_TOKEN`。詳見 `.env.example`。

**建議設定 — Cloudflare Workers AI 後備。** 設定 `CLOUDFLARE_ACCOUNT_ID` 與 `CLOUDFLARE_API_TOKEN`（token 只需要 **Account > Workers AI > Read** 權限），可為情緒分析這一段提供架在不同計費管道上的免費後備。未設定時該段只剩 Hugging Face，額度一旦耗盡整段會同時失效。`HF_SENTIMENT_MODEL` 可在不改程式碼的情況下覆寫主要模型。

**選用 — 透過訂閱使用 Claude Sonnet。** 若要透過 Claude Agent SDK 在 Claude **Sonnet** 上執行風險／出場關卡 — 運用您的 **Claude Pro 方案內含額度**而非按用量計費的 Haiku API token — 請新增 `CLAUDE_CODE_OAUTH_TOKEN`（來自 `claude setup-token`）。可微調參數：`CLAUDE_SDK_FOR`（`exits` [預設] | `all` | `none`）與 `CLAUDE_SDK_MODEL`（預設 `sonnet`）。若無 token，機器人將如以往般僅以 Haiku 執行。

**選用 — 透過 deeperseeker 使用 DeepSeek。** 設定 `USE_DEEPSEEK=1` 後，DeepSeek V4.1 Flash 會成為分析師與出場分析師的第一段。它是透過 *deeperseeker* 存取的，這是一個本機、相容 OpenAI 介面的反向代理（預設 `http://127.0.0.1:4000/v1`），背後接的是**拋棄式的 DeepSeek 網頁版聊天帳號**。這種用法違反 DeepSeek 的服務條款（ToS），因此帳號隨時可能被封鎖、token 也隨時可能失效；兩者都會以 HTTP 錯誤（401/403/429/5xx）的形式出現。該代理在並行請求下會掛掉，所以 DeepSeek 的呼叫一律**循序**進行。每次執行都有一個**斷路器（circuit breaker）**：連續失敗 3 次之後，該次執行剩餘時間不再呼叫 DeepSeek，把一次故障的代價限制在幾次逾時之內。後備順序：DeepSeek → agy → Gemini API 鏈；DeepSeek 掛掉時，機器人損失的只是主要的那一段。可微調參數：`DEEPSEEK_BASE`（預設 `http://127.0.0.1:4000/v1`）、`DEEPSEEK_MODEL`（預設 `v4.1flash`）、`DEEPSEEK_API_KEY`（`USE_DEEPSEEK=1` 時必填）、`DEEPSEEK_TIMEOUT`（預設 75 秒）。這與情緒分析那一段使用的 Hugging Face `deepseek-ai/DeepSeek-V4.1-Flash`（第 3 步）是兩回事。無論由哪個模型回答，JSON 欄位為了與既有日誌相容，仍沿用 `gemini_*` 名稱。

**選用 — 透過 Antigravity 訂閱使用 Gemini。** 設定 `USE_AGY_GEMINI=1` 可讓分析師與出場分析師改走 `agy` CLI，使用 Google AI 訂閱額度而非 Gemini API 金鑰。該額度以運算量計量、**每週**重置，因此耗盡後是數日的鎖定而非隔日恢復；agy 的任何失敗都會自動退回 API 鏈。若同時啟用 DeepSeek，agy 是第二段，僅在 DeepSeek 失敗後才會嘗試。可微調參數：`AGY_MODEL`（預設 `Gemini 3.8 Flash (High)`）、`AGY_BIN`、`AGY_TIMEOUT`。

**選用 — 額度閥門。** 設定 `EXIT_GATE=1` 後，僅有股價低於 5 日均線的部位才會進行 LLM 出場複核。以 19,654 筆歷史複核實測：可減少約 65% 的出場分析師呼叫，同時仍能觸發 73% 的實際成交出場；市場資料不可用時採 fail-open（照常複核）。預設關閉 — 只有當瓶頸是額度而非準確度時，這筆交換才划算。

**成本記錄。** 每次計費的 Claude 呼叫都會附加寫入 `trade_logs/llm_calls.jsonl`，包含模型、路徑、呼叫類型、token 數與成本。`llm_cost_report.py` 可彙整這份記錄（`--days N` 逐日拆解，`--days 0` 讀取整份檔案）。計費與訂閱兩類呼叫分開列示，絕不加總。出場複核另會存下當次實際送出的提示詞，使該元件日後可直接評估，而非事後從其他日誌重建。

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

請以相同頻率啟用 `healthcheck.py`。它刻意放在外部：促成它的那次故障，是 `trader.py` 在 *import* 階段就當掉，任何行程內的防護都來不及執行，因此唯一可靠的訊號就是最近是否有一次執行跑完。它有兩個彼此獨立的警報：**stale**（過期），即開盤時間內 `run_log.jsonl` 不再更新；以及 **degraded**（降級），即一次執行雖然完成，但整條情緒分析模型鏈全部失效，所有標的都被評為 NEUTRAL/0。第二個警報有頻率限制，並且刻意不動 stale 標記與結束碼，因為機器人還活著，只是在盲飛。

第三個獨立警報負責 DeepSeek（僅在該次執行回報已啟用 DeepSeek 時）。上一次執行中斷路器跳脫時，healthcheck 會送出附帶最後一個錯誤的 **DeepSeek down** 訊息；若錯誤訊息中含有 `HTTP 401` 或 `HTTP 403`（trader.py 的錯誤格式為 `deepseek HTTP 401: ...`），還會附上提示：userToken 很可能已過期或被封鎖，需要重新驗證（re-auth）；其他錯誤（例如逾時，或提到 `max_tokens` 的訊息）則不附提示。它使用自己的標記檔 `trade_logs/DEEPSEEK_DOWN`，因此與 degraded 警報各自限頻（同為 12 小時窗口）。

標記檔只有在警報確實送達（或根本沒有設定任何通道）時才會寫入，所以所有通道（ntfy、webhook、電子郵件）都失敗時，下一次執行會重送，而不是靜默 12 小時；不論送達與否，`healthcheck.log` 都會寫入一行。degraded 警報也是同樣的行為。DeepSeek 恢復回應時，healthcheck 會送出一次 **DeepSeek recovered** 訊息，但必須是一次乾淨的執行（至少成功呼叫一次、沒有任何失敗，且斷路器未跳脫）。標記檔不會被刪除，而是改寫成 `<原警報時間戳> RECOVERED`：原本的時間戳繼續替 down 警報限頻，`RECOVERED` 標記則讓 recovered 訊息每次故障只會送一次，因此時好時壞的 DeepSeek（掛、好、掛……）每 12 小時最多只會發一次警報，且同一次故障最多只送一則 recovered。窗口過後若再次發出警報，會覆寫標記並重新啟用下一則 recovered 訊息。沒有呼叫過 DeepSeek、或仍有失敗的執行不會動這個標記。與 degraded 警報一樣，它不會動 stale 標記或結束碼，因為機器人已經退回 agy/Gemini 並繼續交易。

```cron
*/30 9-15 * * 1-5 HEALTHCHECK_NTFY_TOPIC=your-topic /usr/bin/python3 /path/to/healthcheck.py
```

可微調參數：`HEALTHCHECK_MAX_AGE_MIN`（預設 90）、`HEALTHCHECK_DEGRADED_REALERT_H`（預設 12；同時也是 DeepSeek 的重複警報窗口）、`HEALTHCHECK_NTFY_TOPIC`、`HEALTHCHECK_WEBHOOK`、`HEALTHCHECK_SMTP_FILE`（下方電子郵件設定檔的路徑）。未設定任何通道時，仍會寫入 `trade_logs/healthcheck.log` 並留下標記檔，不會對外發送任何訊息；限頻標記檔照樣會寫入，因此不會重複。

**電子郵件（Gmail SMTP）。** 只要存在僅限 root 讀取的設定檔，每一則警報（stale、degraded、DeepSeek down、recovered）都會同時寄出電子郵件。設定放在檔案而不是環境變數，是因為 crontab 會出現在列表輸出中，密碼也會跟著外洩。路徑為 `$HEALTHCHECK_SMTP_FILE`，預設 `/root/.healthcheck-smtp`；檔案不存在時，電子郵件通道就是關閉的。

```ini
# /root/.healthcheck-smtp  (chmod 600)
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=you@gmail.com
SMTP_PASS="abcd efgh ijkl mnop"
SMTP_TO=you@gmail.com, other@example.com
SMTP_FROM=you@gmail.com
```

```bash
chmod 600 /root/.healthcheck-smtp
python3 /path/to/healthcheck.py --test-email
```

`SMTP_HOST`（預設 `smtp.gmail.com`）、`SMTP_PORT`（預設 587，使用 STARTTLS；465 則使用隱式 TLS）與 `SMTP_FROM`（預設為 `SMTP_USER`）皆為選填；`SMTP_USER`、`SMTP_PASS` 與 `SMTP_TO`（可用逗號分隔多個收件人）為必填，缺任何一個，電子郵件通道就維持關閉。`SMTP_PASS` 是 Google 應用程式密碼（需啟用兩步驟驗證），Google 顯示時所帶的空格會被自動移除。以 `#` 開頭的行視為註解，值可以加引號。`--test-email` 會透過所有已設定的通道送出一則測試訊息、印出哪些通道成功送達，只要至少有一個成功就以結束碼 0 離開，否則為 1；它不會寫入任何標記檔。寄信失敗時，`healthcheck.log` 會記錄一行 `EMAIL_FAIL <錯誤>`（絕不包含密碼）；只要 ntfy 或 webhook 已送出該警報，仍視為已送達。

不支援匿名的 ntfy 電子郵件：ntfy.sh 會以 HTTP 400（`anonymous email sending is not allowed`）拒絕 `Email:` 標頭，連推播也會一併失敗，因此 `HEALTHCHECK_NTFY_EMAIL` 已移除。請改用上述的 SMTP 設定檔。

## 儀表板

位於 `dashboard/` 的小型 Flask 應用程式，用於顯示倉位、決策與歷史紀錄，提供**繁體中文與英文**兩種語言（於 `/lang/<code>` 切換，字串集中在 `i18n.py`）。總覽頁面繪製投資組合價值隨時間變化的圖表，並帶有**歷史高點線與回撤陰影**，持倉表格也已併入該頁 —— `/positions` 保留為導向 `/` 的轉址，舊連結仍然可用。

頁尾與決策卡片上的模型名稱是在算繪當下從 log 推導出來的，不是手寫的，因此不可能與機器人實際呼叫的模型脫節。標籤描述的是**角色**（`Sentiment`）而非**廠商**，理由相同：那一段背後的供應商會換。分析師徽章（`DeepSeek: BUY`、`Gemini: HOLD`）是唯一點名廠商的地方，而且點的是實際回答的供應商，依每一列的模型標籤判斷（`ds:` → DeepSeek，`agy:`、`cli:` 或 `gemini…` → Gemini；空白、`none` 或其他 → 通用的 `Analyst`）；靜態的欄位標題則一律只寫 `Analyst`。

```bash
python3 dashboard/app.py
```

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
