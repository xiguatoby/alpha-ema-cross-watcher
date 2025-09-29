# Alpha EMA Cross Watcher

监控指定代币在 **15m**（或自定义）周期发生 **EMA144/EMA576 严格金叉** 并推送到 Telegram。

## 功能
- ✅ OKX Web3 DEX /api/v5/dex/market/candles 拉取 K 线  
- ✅ 计算 EMA（默认 144、576，可配置）
- ✅ 检测最近 3 根 K 线的严格金叉（前一根≤且当前根＞）
- ✅ Telegram 推送（Markdown）
- ✅ `.env` 配置密钥与代理；日志落盘；优雅退出

## 快速开始
```bash
git clone https://github.com/<you>/alpha-ema-cross-watcher.git
cd alpha-ema-cross-watcher
python -m venv .venv && source .venv/bin/activate  # Windows 用 .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# 编辑 .env 写入你的 OKX/TG 信息与监控列表
python watcher.py
```

## 环境变量
见 `.env.example`，**不要把真实密钥提交到仓库**。

## License
MIT
