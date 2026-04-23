# Celestial Blueprint — Setup Guide

## What you have
```
astro_app/
├── app.py              ← Flask backend (chart calculation + AI generation)
├── templates/
│   └── index.html      ← Frontend (form + report display)
├── requirements.txt    ← Python dependencies
└── README.md           ← This file
```

## Step 1 — Install Python
Make sure you have Python 3.10+ installed.
Check with: `python3 --version`

Download from: https://www.python.org/downloads/

## Step 2 — Create a virtual environment
```bash
cd astro_app
python3 -m venv venv
source venv/bin/activate        # Mac/Linux
# OR
venv\Scripts\activate           # Windows
```

## Step 3 — Install dependencies
```bash
pip install -r requirements.txt
```

## Step 4 — Add your Anthropic API key
Get your key from: https://console.anthropic.com/

Set it as an environment variable:
```bash
export ANTHROPIC_API_KEY=sk-ant-your-key-here   # Mac/Linux
# OR
set ANTHROPIC_API_KEY=sk-ant-your-key-here       # Windows
```

Or create a `.env` file in the astro_app folder:
```
ANTHROPIC_API_KEY=sk-ant-your-key-here
```
And install python-dotenv: `pip install python-dotenv`
Then add to the top of app.py:
```python
from dotenv import load_dotenv
load_dotenv()
```

## Step 5 — Run the app
```bash
python3 app.py
```

Then open your browser and go to:
**http://localhost:5000**

## Cost per report
Each report generation uses ~2,500 tokens output + ~1,500 input.
At Claude claude-opus-4-5 pricing this is approximately $0.06–0.10 per report.
At $9 per report, your margin is ~98%.

## Next steps (Phase 3)
- Add Stripe payment before generation
- Deploy to Railway or Render (free tier available)
- Add a landing page explaining the product
- Collect email + deliver report via email as PDF

## Troubleshooting
**"Could not find location"** — Try adding the country name,
e.g. "Bergen, Norway" instead of just "Bergen"

**API key error** — Make sure ANTHROPIC_API_KEY is set correctly

**Port already in use** — Change port in app.py: `app.run(port=5001)`
