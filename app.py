import os, json, warnings, threading, io, base64
warnings.filterwarnings("ignore")
from flask import Flask, request, jsonify, Response, render_template
from kerykeion.astrological_subject_factory import AstrologicalSubjectFactory
from kerykeion.aspects import AspectsFactory
import anthropic
import requests

app = Flask(__name__)

ALL_SIGNS = ["Ari","Tau","Gem","Can","Leo","Vir","Lib","Sco","Sag","Cap","Aqu","Pis"]
SIGN_NAMES = {"Ari":"Aries","Tau":"Taurus","Gem":"Gemini","Can":"Cancer","Leo":"Leo","Vir":"Virgo","Lib":"Libra","Sco":"Scorpio","Sag":"Sagittarius","Cap":"Capricorn","Aqu":"Aquarius","Pis":"Pisces"}
RULERS = {"Ari":"Mars","Tau":"Venus","Gem":"Mercury","Can":"Moon","Leo":"Sun","Vir":"Mercury","Lib":"Venus","Sco":"Mars","Sag":"Jupiter","Cap":"Saturn","Aqu":"Uranus","Pis":"Neptune"}
HOUSE_NAMES = {"First_House":"1st","Second_House":"2nd","Third_House":"3rd","Fourth_House":"4th","Fifth_House":"5th","Sixth_House":"6th","Seventh_House":"7th","Eighth_House":"8th","Ninth_House":"9th","Tenth_House":"10th","Eleventh_House":"11th","Twelfth_House":"12th"}


def calculate_chart(name, year, month, day, hour, minute, lat, lng, tz_str):
    s = AstrologicalSubjectFactory.from_birth_data(
        name=name, year=year, month=month, day=day, hour=hour, minute=minute,
        lng=lng, lat=lat, tz_str=tz_str,
        zodiac_type="Tropical", houses_system_identifier="W",
        online=False, suppress_geonames_warning=True
    )

    asc_sign = s.first_house.sign
    asc_idx = ALL_SIGNS.index(asc_sign)
    ws_houses = [ALL_SIGNS[(asc_idx+i)%12] for i in range(12)]

    def hn(h): return HOUSE_NAMES.get(h, h)
    def fs(a): return SIGN_NAMES.get(a, a)

    planets_raw = {"Sun":s.sun,"Moon":s.moon,"Mercury":s.mercury,"Venus":s.venus,"Mars":s.mars,"Jupiter":s.jupiter,"Saturn":s.saturn,"Uranus":s.uranus,"Neptune":s.neptune,"Pluto":s.pluto}
    pd = {pn:{"sign":fs(p.sign),"house":hn(p.house),"position":round(p.position,2)} for pn,p in planets_raw.items()}

    nn, sn = s.true_north_lunar_node, s.true_south_lunar_node
    pd["North Node"] = {"sign":fs(nn.sign),"house":hn(nn.house),"position":round(nn.position,2)}
    pd["South Node"] = {"sign":fs(sn.sign),"house":hn(sn.house),"position":round(sn.position,2)}
    pd["Chiron"] = {"sign":fs(s.chiron.sign),"house":hn(s.chiron.house),"position":round(s.chiron.position,2)}

    pof = (s.first_house.abs_pos + s.moon.abs_pos - s.sun.abs_pos) % 360
    pof_sign = ALL_SIGNS[int(pof//30)]
    pof_house = ws_houses.index(pof_sign)+1 if pof_sign in ws_houses else "?"

    mc, ic = s.medium_coeli, s.imum_coeli
    mc_ws_house = ws_houses.index(mc.sign)+1 if mc.sign in ws_houses else "?"
    ic_ws_house = ws_houses.index(ic.sign)+1 if ic.sign in ws_houses else "?"

    angles = {
        "ASC":{"sign":fs(asc_sign),"position":round(s.first_house.position,2)},
        "MC":{"sign":fs(mc.sign),"position":round(mc.position,2),"ws_house":mc_ws_house},
        "IC":{"sign":fs(ic.sign),"position":round(ic.position,2),"ws_house":ic_ws_house},
    }

    hr = {h:{"sign":fs(ws_houses[h-1]),"ruler":RULERS[ws_houses[h-1]]} for h in [1,2,3,6,10,11]}

    result = AspectsFactory.single_chart_aspects(s)
    career = {"Sun","Moon","Mercury","Venus","Mars","Jupiter","Saturn","True_North_Lunar_Node","True_South_Lunar_Node","Ascendant","Medium_Coeli","Imum_Coeli","Chiron"}
    aspects = []
    seen = set()
    for a in result.aspects:
        p1, p2 = a.p1_name, a.p2_name
        key = tuple(sorted([p1,p2])+[a.aspect])
        if key in seen: continue
        seen.add(key)
        if p1 in career or p2 in career:
            aspects.append({
                "p1": p1.replace("_"," ").replace("True ","").replace("Mean ",""),
                "aspect": a.aspect,
                "p2": p2.replace("_"," ").replace("True ","").replace("Mean ",""),
                "orb": round(abs(a.orbit),2)
            })
    aspects.sort(key=lambda x:x["orb"])

    return {
        "name": name,
        "planets": pd,
        "angles": angles,
        "house_rulers": hr,
        "ws_houses": [fs(s) for s in ws_houses],
        "part_of_fortune": {"sign":fs(pof_sign),"house":pof_house},
        "aspects": aspects
    }


def build_prompt(chart, birth_info, preview_only=False):
    pd = chart["planets"]
    a = chart["angles"]
    hr = chart["house_rulers"]
    aspects = chart["aspects"]
    pof = chart["part_of_fortune"]

    planet_lines = [f"  - {n}: {d['sign']}, {d['house']} house, {d['position']}°" for n,d in pd.items()]
    aspect_lines = [f"  - {x['p1']} {x['aspect']} {x['p2']} (orb: {x['orb']}°)" for x in aspects[:20]]
    ruler_lines = [f"  - {h}th house ({hr[h]['sign']}) ruler: {hr[h]['ruler']} — in {pd.get(hr[h]['ruler'],{}).get('sign','?')} {pd.get(hr[h]['ruler'],{}).get('house','?')} house" for h in [1,2,6,10,11]]

    chart_data = f"""BIRTH DETAILS: {chart['name']}, {birth_info['date']}, {birth_info['time']}, {birth_info['city']}, {birth_info['country']}
House System: Whole Sign

PLANETS:
{chr(10).join(planet_lines)}

ANGLES:
  - ASC: {a['ASC']['sign']} {a['ASC']['position']}°
  - MC: {a['MC']['sign']} {a['MC']['position']}° (Whole Sign house {a['MC']['ws_house']})
  - IC: {a['IC']['sign']} {a['IC']['position']}° (Whole Sign house {a['IC']['ws_house']})

HOUSE RULERS:
{chr(10).join(ruler_lines)}

PART OF FORTUNE: {pof['sign']} in house {pof['house']}

KEY ASPECTS (tightest first):
{chr(10).join(aspect_lines)}"""

    if preview_only:
        return f"""You are a professional astrologer writing a single opening paragraph called "Soul's Signature" for a premium birth chart report. Warm, wise, direct, poetic but grounded. Second person.

{chart_data}

Write ONLY this one section. 4-5 sentences. Capture the essence of who this person is at their core — their most fundamental energy, the quality they carry into every room. Weave together Sun, Moon, ASC and the 2-3 tightest aspects. Make it feel like the most accurate thing anyone has ever said about them. Output only the paragraph content — no heading, no preamble."""

    return f"""You are a professional astrologer writing a premium, deeply personal Life Purpose, Career & Business Blueprint Report. Warm, wise, direct tone. Second person. No jargon — only meaning. Every sentence must feel specific to this person. Be rich and detailed — this is a paid premium report.

{chart_data}

Write the report using EXACTLY these eight sections with ## headers. Go deep. Use ### sub-headings as specified below. Every sub-section must have at least 1 full paragraph. When interpreting any house, always cover BOTH the sign on the cusp (what colour of energy flows through it) AND the planets in it (what is being expressed there).

## Your Soul's Signature
4-5 sentences. A powerful, poetic portrait capturing the essence of who this person is at their core. Weave together Sun, Moon, ASC and the 2-3 tightest aspects.

## Your IC — Where You Come From
3 paragraphs. IC sign and Whole Sign house placement, emotional foundation, early environment. The IC-to-MC axis as the defining arc of life. Include planets conjunct IC or MC.

## Your Life Purpose
4 paragraphs. North and South Node — signs, houses, what axis reveals about soul's direction. Include aspects to nodes. What to move toward, what pattern to release.

## Your Career Path & Calling
Use these EXACT ### sub-headings, one paragraph each:

### The 10th House: Your Vocation
Sign on the 10th house cusp (the energy of their calling) AND any planets in the 10th house (what is being expressed publicly). Cover both fully.

### The 6th House: Your Daily Work
Sign on the 6th house cusp AND any planets there. What daily work environment and rhythm suits them.

### The 2nd House: Money and Values
Sign on the 2nd house cusp AND any planets there. Their relationship with money, values, and material security.

### The Career Ruler: Where Your Career Energy Flows
The ruler of the 10th house sign — where it sits, its sign and house placement, its aspects. What this reveals about where career energy actually plays out.

### The MC: Your Public Reputation
MC sign AND the Whole Sign house it falls into. What they will become publicly known for.

### Careers That Fit Your Chart
List 5-6 specific real-world career examples with 1-2 sentences explaining why each fits. Use this format for each: start with the career name in bold on its own line, then a short explanation paragraph below.

## Your Unique Gifts
3 paragraphs. Benefic aspects to personal planets, Moon, 9th house, Chiron as wound-become-gift, Part of Fortune, Venus/Jupiter aspects. Name each gift and explain where it comes from.

## Your Greatest Challenge
2-3 paragraphs. Difficult aspects under 5° orb, Saturn placement, South Node shadow, 12th house. Frame as invitation.

## Your Business & Personal Brand Blueprint
Use these EXACT ### sub-headings, one paragraph each:

### Brand Identity & Aesthetic
Draw on ASC sign, 10th house sign and planets, Venus sign and house. What visual and energetic signature should their brand carry?

### Content Style & Voice
Draw on Mercury sign and house, 3rd house, Moon. What content formats and topics give them natural authority?

### Audience & Community Growth
Draw on 11th house sign and planets, Jupiter placement, North Node. Who is drawn to them and how do they grow a loyal following?

### Monetisation & Income Streams
Draw on 2nd house, 8th house, Venus aspects. Best income models that match their chart.

### Platform Fit
Which social platforms genuinely suit this chart and why (Instagram, TikTok, YouTube, Podcast, LinkedIn, Substack)?

## A Message From Your Chart
1 powerful closing paragraph. Reference the most exact aspect. Direct, personal, luminous. Unforgettable.

## Your First Three Steps
A focused call-to-action section. Based on this specific chart, give them THREE concrete, practical actions they can take within the next 30 days to start living more aligned with their blueprint. Use this exact format:

### Step One: [short action title, 3-5 words]
2-3 sentences explaining what to do and why it matches their chart specifically (reference a placement or aspect).

### Step Two: [short action title, 3-5 words]
2-3 sentences explaining what to do and why it matches their chart specifically.

### Step Three: [short action title, 3-5 words]
2-3 sentences explaining what to do and why it matches their chart specifically.

Make these actions specific and executable — not "reflect on your purpose" but "open a Google Doc and write for 15 minutes about X" or "post one piece of content this week about Y" or "have a conversation with Z about W". Tie each step to the signatures in their chart. Range across: something internal/reflective, something creative/expressive, something external/relational.

FORMATTING RULES — FOLLOW STRICTLY:
- Start directly with "## Your Soul's Signature". No title like "# Report For [Name]".
- Do NOT use horizontal rules (no ---, no ***, no ___).
- Do NOT use **bold text** as a sub-heading. Use ### instead.
- Use ## only for the eight main section headings. Use ### for sub-sections exactly as specified above.
- Regular prose paragraphs only. No numbered lists in running prose ("(1) X, (2) Y") — use ### sub-headings instead.
- For the career examples list, put each career name on its own line as bold (**Name**) followed by the explanation.

Content rules: Whole Sign houses throughout. Tightest aspects = most weight. Every sentence tied to specific placements."""


def generate_full_report(prompt):
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY",""))
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=16000,
        messages=[{"role":"user","content":prompt}]
    )
    return msg.content[0].text


def markdown_to_html(text):
    """Convert simple markdown to HTML, stripping unwanted formatting."""
    import re

    # Strip any top-level single # headers (like "# Life Purpose Report For Lena")
    text = re.sub(r'^#\s+[^\n]+\n', '', text, flags=re.MULTILINE)
    # Strip horizontal rules (---)
    text = re.sub(r'^---+\s*$', '', text, flags=re.MULTILINE)
    # Strip "For [Name]" lines at the top
    text = re.sub(r'^#+\s*For\s+\w+\s*$', '', text, flags=re.MULTILINE)

    html_parts = []
    current_para = []
    in_special = None

    def flush_para():
        nonlocal current_para
        if current_para:
            para_text = " ".join(current_para)
            # Convert **bold** to <strong>bold</strong>
            para_text = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', para_text)
            # Convert *italic* to <em>italic</em>
            para_text = re.sub(r'(?<!\*)\*([^*]+)\*(?!\*)', r'<em>\1</em>', para_text)
            html_parts.append("<p>" + para_text + "</p>")
            current_para = []

    for line in text.split("\n"):
        stripped = line.strip()

        # Skip empty lines (with flush)
        if stripped == "":
            flush_para()
            continue

        # Main ## headers
        if stripped.startswith("## "):
            flush_para()
            if in_special:
                html_parts.append("</div>")
                in_special = None
            heading = stripped[3:].strip()
            # Strip ** from heading too
            heading = re.sub(r'\*\*([^*]+)\*\*', r'\1', heading)
            is_message = "message" in heading.lower()
            is_business = "business" in heading.lower()
            is_steps = "first three steps" in heading.lower() or "first 3 steps" in heading.lower()
            if is_message:
                html_parts.append(f'<div class="message-callout"><h2>{heading}</h2>')
                in_special = "msg"
            elif is_business:
                html_parts.append(f'<div class="business-section"><h2>{heading}</h2>')
                in_special = "biz"
            elif is_steps:
                html_parts.append(f'<div class="steps-section"><h2>{heading}</h2>')
                in_special = "steps"
            else:
                html_parts.append(f"<h2>{heading}</h2>")
            continue

        # Sub ### headers or **Bold** lines used as subheadings
        if stripped.startswith("### "):
            flush_para()
            sub_heading = stripped[4:].strip()
            sub_heading = re.sub(r'\*\*([^*]+)\*\*', r'\1', sub_heading)
            html_parts.append(f"<h3>{sub_heading}</h3>")
            continue

        # Lines that are only **bold** text act as sub-headings
        bold_only = re.match(r'^\*\*([^*]+)\*\*:?\s*$', stripped)
        if bold_only:
            flush_para()
            html_parts.append(f"<h3>{bold_only.group(1)}</h3>")
            continue

        current_para.append(stripped)

    flush_para()
    if in_special:
        html_parts.append("</div>")

    return "\n".join(html_parts)


def build_email_body_html(name):
    """Simple warm personal email body with PDF attached separately."""
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f8f3ec;font-family:'EB Garamond',Georgia,serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f8f3ec;padding:60px 20px;">
<tr><td align="center">
  <table width="560" cellpadding="0" cellspacing="0" style="max-width:560px;">

    <tr><td style="text-align:center;padding-bottom:40px;">
      <div style="color:#b8905a;letter-spacing:0.35em;font-size:14px;">✦</div>
    </td></tr>

    <tr><td style="font-family:Georgia,serif;font-size:18px;line-height:1.8;color:#1c1713;text-align:left;">
      <p style="margin:0 0 24px;">Dear {name},</p>

      <p style="margin:0 0 24px;">Thank you so much for ordering your Celestial Blueprint — your complete Life Purpose, Career & Business Blueprint report is attached as a PDF.</p>

      <p style="margin:0 0 24px;">Take a moment to read it somewhere quiet where you can let it land. My hope is that it reflects something true about you, and perhaps puts words to things you have always sensed but never quite named.</p>

      <p style="margin:0 0 24px;">I am so grateful for your trust and support. If the reading resonates, I would love to hear from you.</p>

      <p style="margin:0 0 8px;">With warmth,</p>
      <p style="margin:0 0 40px;font-style:italic;">Lena ✦</p>
    </td></tr>

    <tr><td style="text-align:center;padding-top:20px;border-top:1px solid rgba(184,144,90,0.25);">
      <div style="color:#b8905a;font-size:11px;letter-spacing:0.3em;text-transform:uppercase;font-family:'Raleway',Arial,sans-serif;">Celestial Blueprint</div>
      <div style="font-size:11px;color:#7a706a;margin-top:6px;font-family:'Raleway',Arial,sans-serif;">Whole Sign houses · Swiss Ephemeris</div>
    </td></tr>

  </table>
</td></tr>
</table>
</body></html>"""


def build_pdf_html(name, report_text, birth_info, chart):
    """Build the styled HTML that becomes the PDF."""
    report_body = markdown_to_html(report_text)

    p = chart["planets"]
    a = chart["angles"]
    cells = [
        ("Rising", a["ASC"]["sign"], "1st"),
        ("Sun", p["Sun"]["sign"], p["Sun"]["house"]),
        ("Moon", p["Moon"]["sign"], p["Moon"]["house"]),
        ("Mercury", p["Mercury"]["sign"], p["Mercury"]["house"]),
        ("Venus", p["Venus"]["sign"], p["Venus"]["house"]),
        ("Mars", p["Mars"]["sign"], p["Mars"]["house"]),
        ("Jupiter", p["Jupiter"]["sign"], p["Jupiter"]["house"]),
        ("Saturn", p["Saturn"]["sign"], p["Saturn"]["house"]),
        ("MC", a["MC"]["sign"], f"H{a['MC']['ws_house']}"),
        ("IC", a["IC"]["sign"], f"H{a['IC']['ws_house']}"),
    ]

    top_row = "".join([
        f'<td>'
        f'<div class="cell-label">{label}</div>'
        f'<div class="cell-value">{value}</div>'
        f'<div class="cell-house">{house}</div>'
        f'</td>'
        for label, value, house in cells[:5]
    ])
    bottom_row = "".join([
        f'<td>'
        f'<div class="cell-label">{label}</div>'
        f'<div class="cell-value">{value}</div>'
        f'<div class="cell-house">{house}</div>'
        f'</td>'
        for label, value, house in cells[5:]
    ])

    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<style>
  @page {{ size: A4; margin: 24mm 20mm; background: #f5ece0; }}
  @import url('https://fonts.googleapis.com/css2?family=EB+Garamond:ital,wght@0,400;0,500;0,600;1,400;1,500&family=Raleway:wght@300;400;500&display=swap');

  * {{ box-sizing: border-box; }}

  html, body {{
    margin: 0;
    padding: 0;
    background: #f5ece0;
    font-family: 'EB Garamond', Georgia, serif;
    color: #1c1713;
  }}

  .page {{
    background: #f5ece0;
  }}

  .cover {{
    text-align: center;
    padding: 40px 0 50px;
    border-bottom: 1px solid rgba(184,144,90,0.3);
    page-break-after: avoid;
  }}

  .sigil {{
    color: #b8905a;
    font-size: 18px;
    margin-bottom: 24px;
  }}

  .cover h1 {{
    font-family: 'EB Garamond', Georgia, serif;
    font-size: 48px;
    font-weight: 400;
    color: #1c1713;
    margin: 0 0 18px;
    letter-spacing: 0.02em;
    line-height: 1.1;
  }}

  .cover h1 em {{
    font-style: italic;
    color: #b8905a;
    font-weight: 400;
  }}

  .cover .name-date {{
    font-family: 'Raleway', sans-serif;
    font-size: 11px;
    letter-spacing: 0.3em;
    text-transform: uppercase;
    color: #7a706a;
    margin-top: 28px;
  }}

  .cover .place {{
    font-family: 'EB Garamond', serif;
    font-style: italic;
    font-size: 14px;
    color: #7a706a;
    margin-top: 4px;
  }}

  .chart-table {{
    width: 100%;
    border-collapse: separate;
    border-spacing: 1px;
    background: rgba(184,144,90,0.2);
    margin: 40px 0 50px;
  }}

  .chart-table td {{
    background: #faf5ee;
    padding: 10px 6px;
    text-align: center;
    width: 20%;
  }}

  .cell-label {{
    font-family: 'Raleway', sans-serif;
    font-size: 8px;
    letter-spacing: 0.2em;
    text-transform: uppercase;
    color: #b8905a;
    font-weight: 500;
    margin-bottom: 3px;
  }}

  .cell-value {{
    font-family: 'EB Garamond', serif;
    font-size: 13px;
    color: #1c1713;
  }}

  .cell-house {{
    font-family: 'Raleway', sans-serif;
    font-size: 8px;
    color: #7a706a;
    margin-top: 2px;
  }}

  .report h2 {{
    font-family: 'EB Garamond', serif;
    font-size: 20px;
    font-style: italic;
    font-weight: 500;
    color: #1c1713;
    margin: 32px 0 12px;
    padding-bottom: 8px;
    border-bottom: 1px solid rgba(184,144,90,0.3);
    page-break-after: avoid;
  }}

  .report h3 {{
    font-family: 'EB Garamond', serif;
    font-size: 13px;
    font-weight: 600;
    font-style: italic;
    color: #1c1713;
    margin: 22px 0 8px;
    padding-bottom: 4px;
    letter-spacing: 0.02em;
    page-break-after: avoid;
  }}

  .report h3::before {{
    content: '✦  ';
    color: #b8905a;
    font-style: normal;
    font-weight: 400;
  }}

  .report p {{
    font-family: 'EB Garamond', Georgia, serif;
    font-size: 12.5px;
    line-height: 1.75;
    color: #3d3530;
    margin: 0 0 12px;
    text-align: left;
  }}

  .report p strong {{
    font-weight: 500;
    color: #1c1713;
  }}

  .message-callout {{
    margin: 36px 0 10px;
    padding: 22px 26px;
    border-left: 2px solid #b8905a;
    background: rgba(255,255,255,0.5);
    page-break-inside: avoid;
  }}

  .message-callout h2 {{
    font-family: 'EB Garamond', serif;
    font-size: 18px;
    font-style: italic;
    color: #1c1713;
    margin: 0 0 12px;
    padding: 0;
    border: none;
  }}

  .message-callout p {{
    font-style: italic;
    color: #1c1713;
  }}

  .business-section {{
    margin: 36px 0 10px;
    padding: 22px 26px;
    border: 1px solid rgba(184,144,90,0.3);
    background: rgba(255,255,255,0.4);
  }}

  .business-section h2 {{
    margin-top: 0;
  }}

  .steps-section {{
    margin: 40px 0 10px;
    padding: 28px 30px;
    background: rgba(184,144,90,0.08);
    border: 1px solid rgba(184,144,90,0.4);
    page-break-inside: avoid;
  }}

  .steps-section h2 {{
    margin-top: 0;
    border-bottom: 1px solid rgba(184,144,90,0.3);
  }}

  .steps-section h3 {{
    font-family: 'Raleway', sans-serif;
    font-size: 10px;
    font-weight: 500;
    letter-spacing: 0.22em;
    text-transform: uppercase;
    color: #b8905a;
    font-style: normal;
    margin: 18px 0 6px;
  }}

  .steps-section h3::before {{
    content: none;
  }}

  .footer {{
    margin-top: 60px;
    padding-top: 24px;
    border-top: 1px solid rgba(184,144,90,0.3);
    text-align: center;
  }}

  .footer-label {{
    font-family: 'Raleway', sans-serif;
    font-size: 10px;
    letter-spacing: 0.3em;
    text-transform: uppercase;
    color: #b8905a;
  }}

  .footer-note {{
    font-family: 'Raleway', sans-serif;
    font-size: 9px;
    color: #7a706a;
    margin-top: 6px;
    letter-spacing: 0.1em;
  }}
</style>
</head>
<body>
<div class="page">

  <div class="cover">
    <div class="sigil">✦</div>
    <h1>Your <em>Celestial Blueprint</em></h1>
    <div class="name-date">{name} · {birth_info['date']} · {birth_info['time']}</div>
    <div class="place">{birth_info['city']}, {birth_info['country']}</div>
  </div>

  <table class="chart-table">
    <tr>{top_row}</tr>
    <tr>{bottom_row}</tr>
  </table>

  <div class="report">
    {report_body}
  </div>

  <div class="footer">
    <div class="footer-label">Celestial Blueprint</div>
    <div class="footer-note">Whole Sign houses · Swiss Ephemeris</div>
  </div>

</div>
</body></html>"""


def send_report_email(to_email, to_name, email_body_html, pdf_bytes):
    """Send email via Resend API with short personal body + PDF attachment."""
    api_key = os.environ.get("RESEND_API_KEY", "")
    if not api_key:
        print("WARNING: No RESEND_API_KEY set")
        return False

    payload = {
        "from": "Celestial Blueprint <hello@lunabylena.com>",
        "to": [to_email],
        "subject": f"Your Celestial Blueprint ✦ {to_name}",
        "html": email_body_html,
    }

    if pdf_bytes:
        pdf_b64 = base64.b64encode(pdf_bytes).decode()
        payload["attachments"] = [{
            "filename": f"{to_name}-celestial-blueprint.pdf",
            "content": pdf_b64,
        }]

    try:
        r = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=30
        )
        if r.status_code == 200:
            print(f"Email sent to {to_email}")
            return True
        else:
            print(f"Resend error {r.status_code}: {r.text}")
            return False
    except Exception as e:
        print(f"Email send failed: {e}")
        return False


def generate_pdf(html_content):
    """Try to generate PDF from HTML. Returns None if PDF generation isn't available."""
    try:
        from weasyprint import HTML
        return HTML(string=html_content).write_pdf()
    except Exception as e:
        print(f"PDF generation skipped: {e}")
        return None


def background_generate_and_send(email, chart, birth_info):
    """Generate full report as PDF and send email with short personal note. Runs in background thread."""
    try:
        prompt = build_prompt(chart, birth_info, preview_only=False)
        report_text = generate_full_report(prompt)
        # Build PDF from styled HTML
        pdf_html = build_pdf_html(chart["name"], report_text, birth_info, chart)
        pdf_bytes = generate_pdf(pdf_html)
        # Build short personal email body
        email_body = build_email_body_html(chart["name"])
        send_report_email(email, chart["name"], email_body, pdf_bytes)
    except Exception as e:
        print(f"Background generation failed: {e}")


def log_customer(name, email, marketing_opt_in, date, city, country):
    """Log customer details to a CSV file for later Kit import.
    When Kit is integrated, this will also push to Kit API directly."""
    import csv
    from datetime import datetime

    log_file = "customers.csv"
    file_exists = os.path.exists(log_file)

    try:
        with open(log_file, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["timestamp", "name", "email", "marketing_opt_in",
                               "birth_date", "birth_city", "birth_country"])
            writer.writerow([
                datetime.now().isoformat(),
                name, email, "yes" if marketing_opt_in else "no",
                date, city, country
            ])
        print(f"Logged customer: {email} (marketing: {marketing_opt_in})")
    except Exception as e:
        print(f"Failed to log customer: {e}")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/generate", methods=["POST"])
def generate():
    data = request.json
    name = data.get("name","").strip() or "the person"
    email = data.get("email","").strip()
    date_str = data.get("date","")
    time_str = data.get("time","")
    city = data.get("city","")
    country = data.get("country","")
    lat = data.get("lat")
    lng = data.get("lng")
    tz_str = data.get("tz")
    marketing_opt_in = bool(data.get("marketingOptIn", False))

    if not email or "@" not in email:
        return jsonify({"error": "Please provide a valid email address."}), 400

    # Log customer with marketing consent status (for future Kit integration)
    log_customer(name=name, email=email, marketing_opt_in=marketing_opt_in,
                 date=date_str, city=city, country=country)

    try:
        year, month, day = [int(x) for x in date_str.split("-")]
        hour, minute = [int(x) for x in time_str.split(":")]
        lat, lng = float(lat), float(lng)
    except Exception as e:
        return jsonify({"error": "Invalid birth details."}), 400

    try:
        chart = calculate_chart(name, year, month, day, hour, minute, lat, lng, tz_str)
    except Exception as e:
        return jsonify({"error": f"Chart calculation failed: {str(e)}"}), 500

    birth_info = {"date": date_str, "time": time_str, "city": city, "country": country}

    # Start background generation for full report + email
    thread = threading.Thread(
        target=background_generate_and_send,
        args=(email, chart, birth_info),
        daemon=True
    )
    thread.start()

    # Stream the preview (Soul's Signature) to the user immediately
    preview_prompt = build_prompt(chart, birth_info, preview_only=True)
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY",""))

    def stream():
        yield f"data: {json.dumps({'type':'chart','data':chart})}\n\n"
        with client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            messages=[{"role":"user","content":preview_prompt}]
        ) as st:
            for text in st.text_stream:
                yield f"data: {json.dumps({'type':'text','content':text})}\n\n"
        yield f"data: {json.dumps({'type':'done','email':email})}\n\n"

    return Response(stream(), mimetype="text/event-stream",
                   headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})


if __name__ == "__main__":
    app.run(debug=False, port=5000)
