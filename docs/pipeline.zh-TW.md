# 流程

每一輪（平日 9:30–16:00 ET，每 30 分鐘一次）都會對每一批股票代碼跑過同一條流程：

1. **行情與新聞** — 用 yfinance 抓價格歷史，用 Alpaca 新聞 API 抓新聞標題。
2. **分析師**（DeepSeek → agy → Gemini）— 給出 BUY/SELL/HOLD 建議，附信心度與理由。設定 `USE_DEEPSEEK=1` 時，會先透過本機反向代理問 DeepSeek V4.1 Flash（見 [透過 deeperseeker 使用 DeepSeek](operations.zh-TW.md)）；接著是 agy（`USE_AGY_GEMINI=1`，Gemini 訂閱額度）；最後是 Gemini API，沿著模型優先順序鏈（`gemini-3.8-flash` → `gemini-3.6-flash` → … → `gemini-3.1-flash-lite`）依序嘗試，某個模型被額度限制住時就自動往下降級。每筆決策都會記下實際回答的模型：`ds:v4.1flash`、`agy:Gemini 3.8 Flash (High)`、`gemini-…`，整批失敗則記 `none`。
3. **情緒分析**（DeepSeek → Cloudflare Workers AI）— BULLISH/BEARISH/NEUTRAL 的第二意見。只有在跟分析師**直接衝突**時（BUY 對上 BEARISH，或 SELL 對上 BULLISH）才會擋單；NEUTRAL（沒有新聞／新聞空白）不會否決。模型鏈是 Hugging Face 的 `deepseek-ai/DeepSeek-V4.1-Flash`，接著是 Cloudflare Workers AI 的 `@cf/mistralai/mistral-small-3.1-24b-instruct` 與 `@cf/meta/llama-4-scout-17b-16e-instruct`。兩個後備刻意放在**不同的計費管道**上：舊的鏈是四個 Hugging Face 模型，額度一用完，四個會同時回 402。Cloudflare 免費層每天 10,000 Neurons，大約 900 次情緒呼叫，所以 Hugging Face 額度見底時這一段還能活著。把答案放進 `reasoning`、`content` 留空的模型在這裡不能用；這是實測排除的，不是看名氣。
4. **風險控管**（Claude）— 最後一道關卡，否決不安全的交易。關鍵的出場決策走 Claude Agent SDK 的 **Sonnet**，用您 **Claude Pro 方案內含的額度**；大量的買入篩選則用 **Haiku** API。沒有設定訂閱 token 時就只用 Haiku。
5. **執行** — Alpaca 模擬單；強制停損底線**與保護利潤的移動停損**都由程式獨立執行，不經過 LLM。
6. **出場分析** — 未平倉部位會由出場分析師與 Claude 出場風險關卡重新評估。出場分析師的順序跟第 2 步一樣：先 DeepSeek（`USE_DEEPSEEK=1`），再用 Antigravity CLI 走 Google AI **訂閱**額度（`USE_AGY_GEMINI=1`），最後是 Gemini API 鏈；任何一段失敗就換下一段，兩個旗標都沒設時直接用 API 鏈。（Google 在 2026-06-18 停用了個人方案層級的 `gemini-cli`；這條路預設關閉 —— 只有 CLI 背後是付費金鑰時才設定 `USE_GEMINI_EXIT_CLI=1`。）

   這道關卡有個不對稱：判定為 `HOLD` 時會在 Claude 關卡之前就返回，所以關卡可以擋掉不該出的場，卻抓不到漏掉的出場。

提示詞規則禁止分析師在沒給新聞標題時自己編新聞 —— 沒有資料就必須明說，不能編。
