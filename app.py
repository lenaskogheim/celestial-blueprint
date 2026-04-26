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

    # Calculate elemental balance with weighted planets
    SIGN_ELEMENT = {
        "Aries":"fire","Leo":"fire","Sagittarius":"fire",
        "Taurus":"earth","Virgo":"earth","Capricorn":"earth",
        "Gemini":"air","Libra":"air","Aquarius":"air",
        "Cancer":"water","Scorpio":"water","Pisces":"water"
    }
    PLANET_WEIGHT = {
        "Sun":3, "Moon":3, "Mercury":2, "Venus":2, "Mars":2,
        "Jupiter":1.5, "Saturn":1.5, "Uranus":1, "Neptune":1, "Pluto":1
    }
    element_count = {"fire":0,"earth":0,"air":0,"water":0}
    for pn, weight in PLANET_WEIGHT.items():
        psign = pd[pn]["sign"]
        elem = SIGN_ELEMENT.get(psign)
        if elem:
            element_count[elem] += weight
    # ASC and MC also contribute (angles strongly shape expression)
    asc_elem = SIGN_ELEMENT.get(angles["ASC"]["sign"])
    mc_elem = SIGN_ELEMENT.get(angles["MC"]["sign"])
    if asc_elem: element_count[asc_elem] += 2
    if mc_elem: element_count[mc_elem] += 1

    total = sum(element_count.values())
    element_pct = {e: round(c/total*100) for e, c in element_count.items()}
    dominant_element = max(element_count, key=element_count.get)
    asc_element = SIGN_ELEMENT.get(angles["ASC"]["sign"], "earth")

    return {
        "name": name,
        "planets": pd,
        "angles": angles,
        "house_rulers": hr,
        "ws_houses": [fs(s) for s in ws_houses],
        "part_of_fortune": {"sign":fs(pof_sign),"house":pof_house},
        "aspects": aspects,
        "element_balance": element_pct,
        "dominant_element": dominant_element,
        "asc_element": asc_element
    }


ELEMENT_LANGUAGE_GUIDE = {
    "earth": """LANGUAGE REGISTER: GROUNDED & PRACTICAL
- Use concrete, sensory language. Talk about what they can do, build, hold, see.
- Lead with practical implications before any abstract or spiritual framing.
- Use words like: build, structure, foundation, craft, refine, body, work, mastery, slow, patient, real, tangible, true.
- AVOID heavily spiritual or mystical phrasing. Phrases like "your soul came here to remember", "cosmic flow", "energetic frequencies" should NOT appear.
- Examples and metaphors should be material: gardening, architecture, craft, the body, ritual as practice rather than ritual as magic.
- This person trusts what they can demonstrate in the world. Speak to that.""",

    "fire": """LANGUAGE REGISTER: BOLD & DIRECT
- Use vivid, energetic language. Speak with conviction.
- Lead with vision, possibility, and what they're here to embody.
- Use words like: spark, ignite, lead, blaze, courage, boldly, visible, alive, radiate, charge, momentum, becoming.
- Pull no punches. Say things directly. Skip the soft preambles and qualifiers.
- Metaphors should be active: lighting fires, climbing peaks, leading the way, going first.
- Avoid being too philosophical or ruminative. Fire wants to MOVE.""",

    "air": """LANGUAGE REGISTER: CLEAR & CONCEPTUAL
- Use precise, intelligent language. Frame insights as ideas and patterns to consider.
- Lead with frameworks, distinctions, and clear reasoning.
- Use words like: pattern, framework, signal, see, articulate, weave, thread, nuance, perspective, conversation, lens.
- Make the wisdom feel like an interesting idea worth turning over, not a prescription.
- Use light wit and wordplay where it fits. Stay clean and lucid.
- Metaphors should be conceptual: maps, mirrors, conversations, networks, signals.
- Avoid heavy emotional or somatic language unless accurate to a placement.""",

    "water": """LANGUAGE REGISTER: SOULFUL & POETIC
- Use evocative, emotionally attuned language. Speak to the felt sense.
- Lead with what something means at the soul level, then translate to the practical.
- Use words like: remember, feel, sense, soul, flow, deep, current, ancestral, sacred, knowing, intimate, tender, quiet.
- Embrace mystical and spiritual phrasing where it fits — this person resonates with it.
- Metaphors should be elemental and somatic: water, dreams, womb, weather, tides, threads of memory.
- Be willing to sit in mystery. Not everything needs to be resolved or made practical."""
}


def build_language_guidance(dominant_element, asc_element, element_balance):
    """Create adaptive language guidance based on chart's elemental signature."""
    parts = []
    parts.append(f"ELEMENT BALANCE: Fire {element_balance['fire']}%, Earth {element_balance['earth']}%, Air {element_balance['air']}%, Water {element_balance['water']}%")
    parts.append(f"DOMINANT ELEMENT: {dominant_element.upper()} (use as primary tone for the report)")
    parts.append(f"RISING SIGN ELEMENT: {asc_element.upper()} (this shapes how the person receives information — match this in your DELIVERY)")
    parts.append("")
    parts.append(ELEMENT_LANGUAGE_GUIDE[dominant_element])

    # If ASC element differs significantly from dominant, blend
    if asc_element != dominant_element:
        parts.append("")
        parts.append(f"BUT — their RISING is {asc_element.upper()}, which means they prefer information delivered in a {asc_element} register even if their overall energy is {dominant_element}. Lean into the {dominant_element} substance, but shape the DELIVERY/PROSE to match {asc_element} sensibilities.")

    # If element balance is very mixed (no element above 40%), advise more neutral language
    max_pct = max(element_balance.values())
    if max_pct < 35:
        parts.append("")
        parts.append("This chart is ELEMENTALLY BALANCED — no single element dominates strongly. Keep the language register more neutral and adaptive. Avoid going too far in any one direction.")

    return "\n".join(parts)


def build_prompt(chart, birth_info, preview_only=False):
    pd = chart["planets"]
    a = chart["angles"]
    hr = chart["house_rulers"]
    aspects = chart["aspects"]
    pof = chart["part_of_fortune"]
    language_guidance = build_language_guidance(
        chart.get("dominant_element", "earth"),
        chart.get("asc_element", "earth"),
        chart.get("element_balance", {"fire":25,"earth":25,"air":25,"water":25})
    )

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
        return f"""You are a professional astrologer writing a single opening paragraph called "Soul's Signature" for a premium birth chart report. Second person. Match the language register precisely to this person's elemental signature.

{language_guidance}

{chart_data}

Write ONLY this one section. EXACTLY 4-5 sentences. Capture the essence of who this person is at their core — the quality they carry into every room. Weave together Sun, Moon, ASC and the 2-3 tightest aspects. Make it feel like the most accurate thing anyone has ever said about them. Output only the paragraph content — no heading, no preamble. Honour the language register above without naming it explicitly."""

    return f"""You are a professional astrologer writing a premium, deeply personal Life Purpose, Career & Business Blueprint Report. Second person. No jargon — only meaning. Every sentence must feel specific to this person. Be rich and detailed — this is a paid premium report.

CRITICAL — ADAPT LANGUAGE TO THIS CHART:
{language_guidance}

The above language register applies throughout the ENTIRE report. Even when discussing practical career advice, frame it in language that matches this person's elemental signature. Two charts with the same placements should receive the same astrological insights but in noticeably different prose registers.

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
3 paragraphs. Identify the central tension in this chart from difficult aspects under 5° orb, Saturn placement, South Node shadow, or 12th house planets. CRITICAL FRAMING RULES for this section:

The first paragraph names the challenge with compassion and specificity. NEVER say there is "something wrong" with this person. NEVER frame the challenge as a fixed limitation, deficit, or thing they are stuck with. Frame it as a recurring pattern, dynamic, or growth-edge.

The second paragraph reveals the gift hidden inside the challenge. Every difficult placement contains a strength being forged. Show how this exact tension, once met consciously, becomes one of their most valuable qualities. Reference the chart specifically.

The third paragraph offers concrete ways to work WITH this energy, not against it. Give them at least 2-3 practical perspectives, practices, or reframes. Empower them as the creator of their own experience. End with a sentence that affirms their capacity to transform this. The reader should close this section feeling more powerful, not less.

Tone: warm, honest, never pitying. Never doom-laden. The challenge is real, AND they are bigger than it.

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

FORMATTING RULES, FOLLOW STRICTLY:
- Start directly with "## Your Soul's Signature". No title like "# Report For [Name]".
- Do NOT use horizontal rules (no ---, no ***, no ___).
- Do NOT use **bold text** as a sub-heading. Use ### instead.
- Use ## only for the eight main section headings. Use ### for sub-sections exactly as specified above.
- Regular prose paragraphs only. No numbered lists in running prose ("(1) X, (2) Y"), use ### sub-headings instead.
- For the career examples list, put each career name on its own line as bold (**Name**) followed by the explanation.

PUNCTUATION RULES, FOLLOW STRICTLY:
- DO NOT use em-dashes (—) anywhere in the report. They make prose feel AI-generated.
- DO NOT use en-dashes (–) for parentheticals.
- Instead, use commas, full stops, semicolons, colons, or parentheses depending on what the sentence needs.
- For a strong pause that would normally use an em-dash, use a comma or full stop. For a parenthetical aside, use commas or parentheses.
- The only place a hyphen is acceptable is between compound words (e.g. "ten-year-old", "well-meaning").

CONSISTENCY RULES — non-negotiable substance that must be covered the same way every time, regardless of which run this is:
- ALWAYS use Whole Sign houses. Never Placidus, Equal, or Koch.
- The tightest aspects (smallest orb) ALWAYS carry the most interpretive weight.
- ALWAYS state explicitly which Whole Sign house the MC and IC fall in.
- For every house section, ALWAYS cover both: (a) the sign on the cusp AND (b) any planets occupying that house. If no planets, say so explicitly and read the house from its ruler.
- ALWAYS state the ruler of the Ascendant sign and where it sits — this person's overall life direction follows it.
- If a person has a stellium (3+ planets in one sign or one Whole Sign house), ALWAYS name it as a stellium and treat it as a defining chart signature.
- ALWAYS reference at least one specific aspect by name with its exact orb (e.g. "Sun sextile Pluto, 0.28°").
- The "Your First Three Steps" section must always have one internal/reflective action, one creative/expressive action, and one external/relational action.

Content rules: Every sentence tied to specific placements. No generic statements that could apply to anyone."""


def generate_full_report(prompt):
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY",""))
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=16000,
        messages=[{"role":"user","content":prompt}]
    )
    return msg.content[0].text


def clean_dashes(text):
    """Strip em-dashes and en-dashes that make text feel AI-generated.
    Used everywhere text flows to user - PDF, email, on-screen preview."""
    import re
    # Em-dash with spaces -> comma + space
    text = re.sub(r'\s*—\s*', ', ', text)
    text = re.sub(r'\s*–\s*', ', ', text)
    # Clean any double commas from substitution
    text = re.sub(r',\s*,', ',', text)
    # Clean comma right before terminal punctuation
    text = re.sub(r',\s*([.!?:;])', r'\1', text)
    return text


def markdown_to_html(text):
    """Convert simple markdown to HTML, stripping unwanted formatting."""
    import re

    # Strip any top-level single # headers (like "# Life Purpose Report For Lena")
    text = re.sub(r'^#\s+[^\n]+\n', '', text, flags=re.MULTILINE)
    # Strip horizontal rules (---)
    text = re.sub(r'^---+\s*$', '', text, flags=re.MULTILINE)
    # Strip "For [Name]" lines at the top
    text = re.sub(r'^#+\s*For\s+\w+\s*$', '', text, flags=re.MULTILINE)

    # Clean em-dashes and en-dashes
    text = clean_dashes(text)

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
    orphans: 3;
    widows: 3;
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
    box-decoration-break: clone;
    -webkit-box-decoration-break: clone;
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
    /* Allow breaking but maintain breathing room when split across pages */
    box-decoration-break: clone;
    -webkit-box-decoration-break: clone;
  }}

  .business-section h2 {{
    margin-top: 0;
  }}

  .steps-section {{
    margin: 40px 0 10px;
    padding: 28px 30px;
    background: rgba(184,144,90,0.08);
    border: 1px solid rgba(184,144,90,0.4);
    box-decoration-break: clone;
    -webkit-box-decoration-break: clone;
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
        # Buffer text so we can clean dashes that span chunk boundaries
        buffer = ""
        with client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            messages=[{"role":"user","content":preview_prompt}]
        ) as st:
            for text in st.text_stream:
                buffer += text
                # Hold back the last few characters in case a dash is forming
                # Flush everything except the last 3 chars
                if len(buffer) > 3:
                    flush = buffer[:-3]
                    buffer = buffer[-3:]
                    cleaned = clean_dashes(flush)
                    yield f"data: {json.dumps({'type':'text','content':cleaned})}\n\n"
        # Flush any remaining buffer
        if buffer:
            cleaned = clean_dashes(buffer)
            yield f"data: {json.dumps({'type':'text','content':cleaned})}\n\n"
        yield f"data: {json.dumps({'type':'done','email':email})}\n\n"

    return Response(stream(), mimetype="text/event-stream",
                   headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})


if __name__ == "__main__":
    app.run(debug=False, port=5000)
