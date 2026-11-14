[English](operations.md) · **繁體中文**

[回到 README](../README.zh-TW.md)

# 營運與設定

*歷史紀錄：這個機器人已經退役，不再排程執行。以下步驟記錄的是當初的跑法。*

## 安裝與金鑰

```bash
pip install -r requirements.txt
cp .env.example ~/.env        # fill in your keys
```

`~/.env` 裡必填的金鑰：`ALPACA_API_KEY`、`ALPACA_SECRET_KEY`、`GEMINI_API_KEY`、`ANTHROPIC_API_KEY`、`HF_API_TOKEN`。
詳見 `.env.example`。

## Cloudflare Workers AI 後備（建議設定）

設定 `CLOUDFLARE_ACCOUNT_ID` 和 `CLOUDFLARE_API_TOKEN`。
token 只需要 **Account > Workers AI > Read** 權限。
這樣情緒分析這一段就多了一個免費後備，而且走的是另一條計費管道。
沒設的話，這一段只剩 Hugging Face，額度一用完就整段一起掛。
`HF_SENTIMENT_MODEL` 可以直接換掉主要模型，不用改程式碼。

## 透過訂閱使用 Claude Sonnet（選用）

這樣風險／出場關卡會改用 Claude Agent SDK 跑 Claude **Sonnet**。
用的是您 **Claude Pro 方案內含的額度**，不是按量計費的 Haiku API token。
加上 `CLAUDE_CODE_OAUTH_TOKEN`（用 `claude setup-token` 取得）就會啟用。

可調整的參數：`CLAUDE_SDK_FOR`（`exits` [預設] | `all` | `none`）與 `CLAUDE_SDK_MODEL`（預設 `sonnet`）。
沒有 token 的話，機器人就只用 Haiku，跟以前一樣。

## 透過 deeperseeker 使用 DeepSeek

*選用。* 設定 `USE_DEEPSEEK=1` 後，DeepSeek V4.1 Flash 會成為分析師與出場分析師的第一段。
它是透過 *deeperseeker* 連上的，這是一個本機的 OpenAI 相容反向代理（預設 `http://127.0.0.1:4000/v1`）。
這個代理背後接的是一個**用完即丟的 DeepSeek 網頁版聊天帳號**。

這種用法違反 DeepSeek 的服務條款，所以帳號隨時可能被封，token 也可能無預警過期。
兩種情況都會以 HTTP 錯誤（401/403/429/5xx）的形式出現。
代理同時處理多個請求就撐不住，所以 DeepSeek 的呼叫一律**循序**進行。
每一輪執行都有**斷路器**：連續失敗 3 次後，這一輪剩下的部分就不再呼叫 DeepSeek。
這樣一次故障頂多只浪費幾次逾時的成本。

後備順序：DeepSeek → agy → Gemini API 鏈。
DeepSeek 掛掉時，機器人只少了第一段。

可調整的參數：`DEEPSEEK_BASE`（預設 `http://127.0.0.1:4000/v1`）、`DEEPSEEK_MODEL`（預設 `v4.1flash`）、`DEEPSEEK_API_KEY`（只要 `USE_DEEPSEEK=1` 就必填）、`DEEPSEEK_TIMEOUT`（預設 75 秒）。

這跟情緒分析那一段在 Hugging Face 上用的 `deepseek-ai/DeepSeek-V4.1-Flash` 是兩回事（見[流程](pipeline.zh-TW.md)第 3 步）。
不管是哪個模型回答，JSON 欄位都沿用 `gemini_*` 的名稱，這是為了讓 log 保持相容。

## 透過 Antigravity 訂閱使用 Gemini

*選用。* 設定 `USE_AGY_GEMINI=1` 後，分析師與出場分析師會改走 `agy` CLI。
用的是 Google AI 訂閱的額度，不是 Gemini API 金鑰。
這份額度是以算力計量，而且每**週**重置，所以用完就會被鎖上好幾天，隔天不會恢復。
只要 agy 出任何問題，就會自動退回 API 鏈。
DeepSeek 也啟用時，agy 是第二段，DeepSeek 失敗後才會輪到它。

可調整的參數：`AGY_MODEL`（預設 `Gemini 3.8 Flash (High)`）、`AGY_BIN`、`AGY_TIMEOUT`。

## 額度閥

*選用。* 設定 `EXIT_GATE=1` 後，LLM 出場檢視只會針對股價低於 5 日均線的部位。
在 19,654 筆歷史檢視上實測，這樣可以省下約 65% 的出場分析師呼叫，同時仍能找出 73% 實際執行的出場。
市場資料不能用時，它會放行（fail open）。
預設是關的：只有瓶頸在額度、準確度還夠用的時候，這筆取捨才划算。

## 成本紀錄

每一次計費的 Claude 呼叫都會附加到 `trade_logs/llm_calls.jsonl`，記下模型、路徑、呼叫類型、token 數與成本。
`llm_cost_report.py` 會彙整這份紀錄（`--days N` 看每日明細，`--days 0` 看整份檔案）。
按量計費與訂閱的呼叫分開報告，不會加總。
出場檢視還會另外存下當時送出的完整提示詞，事後要評估這個元件時，不必從其他 log 重新拼湊。

## 執行

```bash
python3 trader.py
```

腳本會自行限制在 9:30–16:00 ET 的開盤時間內（並透過 Alpaca 的 clock 端點跳過休假日），所以市場休市時會提早結束。

## Cron（開盤時段每 30 分鐘一次）

```cron
*/30 9-15 * * 1-5 flock -n /tmp/trader.lock /usr/bin/python3 /path/to/trader.py >> /path/to/trade_logs/cron.log 2>&1
```

`flock -n` 可以避免某一輪跑太慢時，後面的執行一直疊上來。
小時範圍請依伺服器的時區調整；不管怎麼設，機器人自己都會限制在 ET 時段內。

## Healthcheck

`healthcheck.py` 請用同樣的頻率排程。
它刻意放在外面：當初促成它的那次故障，是 `trader.py` 在 *import* 階段就崩了，任何程式內的防護都來不及跑。
所以唯一可靠的訊號，就是最近有沒有一輪跑完。

它有兩個彼此獨立的警報：

- **Stale（停滯）**：開盤時段內，`run_log.jsonl` 沒有再更新。
- **Degraded（降級）**：這一輪有跑完，但整條情緒分析鏈都失敗，每檔股票都被評成 NEUTRAL/0。

降級警報有頻率限制。
它刻意不動停滯標記，也不影響結束碼，因為機器人還活著，只是在盲飛。

```cron
*/30 9-15 * * 1-5 HEALTHCHECK_NTFY_TOPIC=your-topic /usr/bin/python3 /path/to/healthcheck.py
```

可調整的參數：`HEALTHCHECK_MAX_AGE_MIN`（預設 90）、`HEALTHCHECK_DEGRADED_REALERT_H`（預設 12；也是 DeepSeek 重新警報的間隔）、`HEALTHCHECK_NTFY_TOPIC`、`HEALTHCHECK_WEBHOOK`、`HEALTHCHECK_SMTP_FILE`（下方電子郵件設定檔的路徑）。
沒有設定任何通道時，它仍會寫 `trade_logs/healthcheck.log` 並留下標記檔，但不會對外送出任何東西。
頻率限制用的標記一樣會寫，所以不會重複警報。

## Healthcheck 標記與重試

標記只有在警報真的送出去（或根本沒設定通道）之後才會寫。
所以每個通道（ntfy、webhook、電子郵件）全部失敗時，下一輪會再試一次，不會就此安靜 12 小時。
`healthcheck.log` 那一行則是不管成敗都會寫。
降級警報也是這樣。

## DeepSeek 警報

第三個獨立的警報負責 DeepSeek（只有在該輪回報它已啟用時才會觸發）。

**DeepSeek 掛了。** 上一輪的斷路器跳開時，healthcheck 會送出 **DeepSeek down** 訊息，附上最後一個錯誤。
如果錯誤文字含有 `HTTP 401` 或 `HTTP 403`（trader.py 把錯誤格式化成 `deepseek HTTP 401: ...`），訊息會多加一句提示：userToken 大概過期或被封了，需要重新驗證。
其他錯誤（例如逾時或 `max_tokens` 的抱怨）就不加提示。
它有自己的標記 `trade_logs/DEEPSEEK_DOWN`，所以頻率限制跟降級警報分開算（同樣是 12 小時的間隔）。

**DeepSeek 恢復了。** DeepSeek 又能回應時，healthcheck 會送出一則 **DeepSeek recovered** 訊息。
但要等到一輪完全正常之後才會送（至少一次成功呼叫、沒有失敗，斷路器也沒跳開）。
標記不會被刪掉，而是改寫成 `<original alert timestamp> RECOVERED`。
原本的時間戳記繼續限制「掛了」警報的頻率。
`RECOVERED` 標籤則讓「恢復了」訊息每次故障只送一次。
所以 DeepSeek 一下掛一下好（掛、好、掛……）時，每 12 小時最多警報一次，同一次故障最多只送一則恢復訊息。
間隔過了之後如果又有新的警報，會取代標記，並為下一則恢復訊息做好準備。
如果某一輪根本沒呼叫過 DeepSeek，或是仍有失敗，標記就維持原樣。

跟降級警報一樣，它不動停滯標記，也不影響結束碼，因為機器人已經退回 agy／Gemini 繼續交易了。

## 電子郵件（Gmail SMTP）

只要有一個僅限 root 讀取的設定檔，每一種警報（停滯、降級、DeepSeek 掛了、恢復了）也會寄電子郵件。
它用檔案而不是環境變數，因為 crontab 會出現在各種列表裡，密碼會跟著外洩。
路徑是 `$HEALTHCHECK_SMTP_FILE`，預設為 `/root/.healthcheck-smtp`。
檔案不存在的話，就是不寄信。

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

`SMTP_HOST`（預設 `smtp.gmail.com`）、`SMTP_PORT`（預設 587，STARTTLS；465 走隱式 TLS）和 `SMTP_FROM`（預設 `SMTP_USER`）是選填。
`SMTP_USER`、`SMTP_PASS` 和 `SMTP_TO`（以逗號分隔）是必填，缺了就不寄信。
`SMTP_PASS` 是 Google 應用程式密碼（需要開啟兩步驟驗證）；Google 顯示的空格會被自動去掉。
以 `#` 開頭的行是註解，值可以加引號。

`--test-email` 會透過每個已設定的通道各送一則測試訊息，並印出哪些通道收下了。
只要有至少一個通道成功就以 0 結束，否則以 1 結束，而且不會寫標記檔。
寄信失敗時，會在 `healthcheck.log` 記成 `EMAIL_FAIL <error>`（絕不會記密碼）。
如果 ntfy 或 webhook 收下了警報，就算送達。

## 不支援 ntfy 的電子郵件

不支援匿名的 ntfy 電子郵件。
ntfy.sh 會用 HTTP 400（`anonymous email sending is not allowed`）拒絕 `Email:` 標頭，連推播也一起丟掉。
所以 `HEALTHCHECK_NTFY_EMAIL` 已經不存在了。
請改用 SMTP 設定檔。

## 儀表板

`dashboard/` 裡有個小型 Flask 應用，用來顯示部位、決策與歷史紀錄，介面有 **zh-TW 與英文**兩種（在 `/lang/<code>` 切換；字串放在 `i18n.py`）。
總覽頁會畫出投資組合價值隨時間的變化，附上**高水位線與回撤陰影**，也放了持股表。
`/positions` 保留為導向 `/` 的轉址，舊連結照樣能用。

頁尾與決策卡片上的模型名稱，是在畫面產生時從 log 推出來的，不是手寫的，所以不會跟機器人實際呼叫的模型脫節。
標籤寫角色（`Sentiment`），不寫廠商，理由相同：那一段背後的供應商會換。
分析師徽章（`DeepSeek: BUY`、`Gemini: HOLD`）是唯一會寫出廠商名稱的地方。
它顯示的是實際回答的供應商，依每一列的模型標籤判斷（`ds:` → DeepSeek，`agy:`、`cli:` 或 `gemini…` → Gemini；空白、`none` 或其他 → 通用的 `Analyst`）。
固定的欄位標題一律只寫 `Analyst`。

```bash
python3 dashboard/app.py
```

trader.hydr0negnetwork.de 上的公開站台已經換成靜態的退役說明頁。
