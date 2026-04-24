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

Write the report using EXACTLY these seven sections with ## headers. Go deep. Minimum 3 paragraphs per major section.

## Your IC — Where You Come From
3 paragraphs. IC sign and Whole Sign house, emotional foundation, early environment. The IC-to-MC axis as the defining arc of life. Include planets conjunct IC or MC.

## Your Life Purpose
4 paragraphs. North and South Node — signs, houses, what axis reveals about soul's direction. Include aspects to nodes. What to move toward, what pattern to release.

## Your Career Path & Calling
5 paragraphs: (1) 10th house sign and planets, (2) 6th house daily work, (3) 2nd house money and values, (4) career ruler placement and aspects, (5) MC sign and Whole Sign house placement. Include 5-6 specific real career examples.

## Your Unique Gifts
3 paragraphs. Benefic aspects to personal planets, Moon, 9th house, Chiron as gift, Part of Fortune, Venus/Jupiter aspects. Name each gift and explain where it comes from.

## Your Greatest Challenge
2-3 paragraphs. Difficult aspects under 5° orb, Saturn placement, South Node shadow, 12th house. Frame as invitation.

## Your Business & Personal Brand Blueprint
5 paragraphs:
1. Brand identity and aesthetic (ASC, 10th house, Venus)
2. Content style and authority topics (Mercury, 3rd house, Moon)
3. Audience and community growth (11th house, Jupiter, North Node)
4. Monetisation and income streams (2nd house, 8th house, Venus aspects)
5. Platform fit (Instagram/TikTok/YouTube/Podcast/LinkedIn based on chart)

## A Message From Your Chart
1 powerful closing paragraph. Reference the most exact aspect. Direct, personal, luminous. Unforgettable.

Rules: Whole Sign houses throughout. Tightest aspects = most weight. Every sentence tied to specific placements."""


def generate_full_report(prompt):
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY",""))
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=16000,
        messages=[{"role":"user","content":prompt}]
    )
    return msg.content[0].text


def markdown_to_html(text):
    """Convert simple markdown (## headers, paragraphs) to HTML."""
    html_parts = []
    current_para = []
    in_special = None

    for line in text.split("\n"):
        if line.startswith("## "):
            if current_para:
                html_parts.append("<p>" + " ".join(current_para) + "</p>")
                current_para = []
            if in_special:
                html_parts.append("</div>")
                in_special = None
            heading = line[3:].strip()
            is_message = "message" in heading.lower()
            is_business = "business" in heading.lower()
            if is_message:
                html_parts.append(f'<div class="message-callout"><h2>{heading}</h2>')
                in_special = "msg"
            elif is_business:
                html_parts.append(f'<div class="business-section"><h2>{heading}</h2>')
                in_special = "biz"
            else:
                html_parts.append(f"<h2>{heading}</h2>")
        elif line.strip() == "":
            if current_para:
                html_parts.append("<p>" + " ".join(current_para) + "</p>")
                current_para = []
        else:
            current_para.append(line.strip())

    if current_para:
        html_parts.append("<p>" + " ".join(current_para) + "</p>")
    if in_special:
        html_parts.append("</div>")

    return "\n".join(html_parts)


def build_email_html(name, report_text, birth_info, chart):
    """Build beautifully styled HTML email with the report."""
    report_body = markdown_to_html(report_text)

    # Chart summary strip
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
    chart_rows = "".join([
        f'<td style="padding:10px 6px;text-align:center;border:1px solid #e8d5b0;background:#ffffff;"><div style="font-size:10px;letter-spacing:0.15em;text-transform:uppercase;color:#b8905a;font-weight:500;margin-bottom:4px;">{label}</div><div style="font-family:Georgia,serif;font-size:14px;color:#1c1713;">{value}</div><div style="font-size:9px;color:#7a706a;margin-top:2px;">{house}</div></td>'
        for label, value, house in cells
    ])

    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f8f3ec;font-family:Georgia,serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f8f3ec;padding:40px 20px;">
<tr><td align="center">
  <table width="640" cellpadding="0" cellspacing="0" style="max-width:640px;background:#ffffff;border-radius:4px;box-shadow:0 4px 40px rgba(0,0,0,0.06);">

    <tr><td style="padding:50px 50px 30px;text-align:center;border-bottom:1px solid #f5ece0;">
      <div style="color:#b8905a;letter-spacing:0.3em;margin-bottom:20px;">✦</div>
      <h1 style="font-family:Georgia,serif;font-size:34px;font-weight:normal;color:#1c1713;margin:0 0 10px;letter-spacing:0.05em;">Your <em style="color:#b8905a;">Celestial Blueprint</em></h1>
      <div style="font-size:11px;letter-spacing:0.3em;text-transform:uppercase;color:#7a706a;margin-top:15px;">{name} · {birth_info['date']} · {birth_info['time']}</div>
      <div style="font-size:11px;color:#7a706a;margin-top:4px;">{birth_info['city']}, {birth_info['country']}</div>
    </td></tr>

    <tr><td style="padding:30px 50px;">
      <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
        <tr>{chart_rows[:len(chart_rows)//2]}</tr>
        <tr>{chart_rows[len(chart_rows)//2:]}</tr>
      </table>
    </td></tr>

    <tr><td style="padding:20px 50px 50px;font-family:Georgia,serif;font-size:16px;line-height:1.8;color:#3d3530;">
      {report_body}
    </td></tr>

    <tr><td style="padding:30px 50px;background:#faf5ee;text-align:center;border-top:1px solid #f5ece0;">
      <div style="color:#b8905a;font-size:12px;letter-spacing:0.3em;text-transform:uppercase;">Celestial Blueprint</div>
      <div style="font-size:11px;color:#7a706a;margin-top:8px;">Whole Sign houses · Swiss Ephemeris</div>
    </td></tr>

  </table>
</td></tr>
</table>

<style>
  .message-callout {{ margin-top:40px !important; padding:25px 30px !important; border-left:3px solid #b8905a !important; background:#faf5ee !important; }}
  .message-callout h2 {{ font-family:Georgia,serif !important; font-size:20px !important; font-style:italic !important; color:#1c1713 !important; margin:0 0 15px !important; }}
  .message-callout p {{ font-style:italic !important; color:#1c1713 !important; }}
  .business-section {{ margin-top:40px !important; padding:25px 30px !important; border:1px solid #f5ece0 !important; background:#ffffff !important; }}
  .business-section h2 {{ font-family:Georgia,serif !important; font-size:22px !important; font-style:italic !important; color:#1c1713 !important; margin:0 0 15px !important; border-bottom:1px solid #f5ece0 !important; padding-bottom:10px !important; }}
  h2 {{ font-family:Georgia,serif !important; font-size:22px !important; font-style:italic !important; color:#1c1713 !important; margin:30px 0 12px !important; padding-bottom:8px !important; border-bottom:1px solid #f5ece0 !important; }}
  p {{ margin-bottom:15px !important; }}
</style>
</body></html>"""


def send_report_email(to_email, to_name, html_content, pdf_bytes):
    """Send email via Resend API."""
    api_key = os.environ.get("RESEND_API_KEY", "")
    if not api_key:
        print("WARNING: No RESEND_API_KEY set")
        return False

    # Encode PDF as base64
    pdf_b64 = base64.b64encode(pdf_bytes).decode() if pdf_bytes else None

    payload = {
        "from": "Celestial Blueprint <onboarding@resend.dev>",
        "to": [to_email],
        "subject": f"Your Celestial Blueprint ✦ {to_name}",
        "html": html_content,
    }

    if pdf_b64:
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
    """Generate full report and send email. Runs in background thread."""
    try:
        prompt = build_prompt(chart, birth_info, preview_only=False)
        report_text = generate_full_report(prompt)
        html = build_email_html(chart["name"], report_text, birth_info, chart)
        pdf_bytes = generate_pdf(html)
        send_report_email(email, chart["name"], html, pdf_bytes)
    except Exception as e:
        print(f"Background generation failed: {e}")


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

    if not email or "@" not in email:
        return jsonify({"error": "Please provide a valid email address."}), 400

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
