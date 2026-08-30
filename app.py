import os, json, warnings, threading, io, base64, functools
warnings.filterwarnings("ignore")
from flask import Flask, request, jsonify, Response, render_template, redirect
from kerykeion.astrological_subject_factory import AstrologicalSubjectFactory
from kerykeion.aspects import AspectsFactory
import anthropic
import requests

app = Flask(__name__)

# Stripe configuration
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
PRICE_EUR = 2700  # €27.00 in cents

ALL_SIGNS = ["Ari","Tau","Gem","Can","Leo","Vir","Lib","Sco","Sag","Cap","Aqu","Pis"]
SIGN_NAMES = {"Ari":"Aries","Tau":"Taurus","Gem":"Gemini","Can":"Cancer","Leo":"Leo","Vir":"Virgo","Lib":"Libra","Sco":"Scorpio","Sag":"Sagittarius","Cap":"Capricorn","Aqu":"Aquarius","Pis":"Pisces"}
TRAD_RULERS = {"Ari":"Mars","Tau":"Venus","Gem":"Mercury","Can":"Moon","Leo":"Sun","Vir":"Mercury","Lib":"Venus","Sco":"Mars","Sag":"Jupiter","Cap":"Saturn","Aqu":"Saturn","Pis":"Jupiter"}
MODERN_RULERS = {"Aqu":"Uranus","Pis":"Neptune","Sco":"Pluto"}
# Backward compat alias
RULERS = TRAD_RULERS
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

    # Build house rulers with TRADITIONAL as primary, MODERN as secondary co-ruler.
    # All 12 houses included so both purpose and love reports can use the same chart data.
    hr = {}
    for h in range(1, 13):
        sign_abbr = ws_houses[h-1]
        hr[h] = {
            "sign": fs(sign_abbr),
            "ruler": TRAD_RULERS[sign_abbr],
            "modern_ruler": MODERN_RULERS.get(sign_abbr),
        }
    # Track MC sign rulers separately (the MC sign may differ from any house cusp's sign in odd cases,
    # but typically equals the 10th house's sign in Whole Sign. We always log the MC sign rulers explicitly
    # so the prompt can guarantee MC ruler coverage with aspects.)
    mc_sign_full = fs(mc.sign)
    mc_sign_abbr = mc.sign  # 3-letter form like "Vir"
    mc_rulers = {
        "sign": mc_sign_full,
        "ruler": TRAD_RULERS.get(mc_sign_abbr),
        "modern_ruler": MODERN_RULERS.get(mc_sign_abbr),
    }

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
        "mc_rulers": mc_rulers,
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
- Embrace mystical and spiritual phrasing where it fits, this person resonates with it.
- Metaphors should be elemental and somatic: water, dreams, womb, weather, tides, threads of memory.
- Be willing to sit in mystery. Not everything needs to be resolved or made practical."""
}


def build_language_guidance(dominant_element, asc_element, element_balance):
    """Create adaptive language guidance based on chart's elemental signature."""
    parts = []
    parts.append(f"ELEMENT BALANCE: Fire {element_balance['fire']}%, Earth {element_balance['earth']}%, Air {element_balance['air']}%, Water {element_balance['water']}%")
    parts.append(f"DOMINANT ELEMENT: {dominant_element.upper()} (use as primary tone for the report)")
    parts.append(f"RISING SIGN ELEMENT: {asc_element.upper()} (this shapes how the person receives information, match this in your DELIVERY)")
    parts.append("")
    parts.append(ELEMENT_LANGUAGE_GUIDE[dominant_element])

    # If ASC element differs significantly from dominant, blend
    if asc_element != dominant_element:
        parts.append("")
        parts.append(f"BUT, their RISING is {asc_element.upper()}, which means they prefer information delivered in a {asc_element} register even if their overall energy is {dominant_element}. Lean into the {dominant_element} substance, but shape the DELIVERY/PROSE to match {asc_element} sensibilities.")

    # If element balance is very mixed (no element above 40%), advise more neutral language
    max_pct = max(element_balance.values())
    if max_pct < 35:
        parts.append("")
        parts.append("This chart is ELEMENTALLY BALANCED, no single element dominates strongly. Keep the language register more neutral and adaptive. Avoid going too far in any one direction.")

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

    # Build "planets in each house" map - which planets ACTUALLY occupy each house
    house_occupants = {h: [] for h in range(1, 13)}
    house_num_map = {"1st":1, "2nd":2, "3rd":3, "4th":4, "5th":5, "6th":6, "7th":7, "8th":8, "9th":9, "10th":10, "11th":11, "12th":12}
    for pname, pdata in pd.items():
        h_num = house_num_map.get(pdata["house"])
        if h_num:
            house_occupants[h_num].append(f"{pname} ({pdata['sign']} {pdata['position']}°)")

    def _ord(n):
        return {1:"1st",2:"2nd",3:"3rd",4:"4th",5:"5th",6:"6th",7:"7th",8:"8th",9:"9th",10:"10th",11:"11th",12:"12th"}.get(n, f"{n}th")

    occupants_lines = []
    asc_idx_h = ["Aries","Taurus","Gemini","Cancer","Leo","Virgo","Libra","Scorpio","Sagittarius","Capricorn","Aquarius","Pisces"].index(a["ASC"]["sign"])
    ws_signs = ["Aries","Taurus","Gemini","Cancer","Leo","Virgo","Libra","Scorpio","Sagittarius","Capricorn","Aquarius","Pisces"]
    for h in range(1, 13):
        sign_on_cusp = ws_signs[(asc_idx_h + h - 1) % 12]
        occupants = house_occupants[h]
        h_ord = _ord(h)
        if occupants:
            occupants_lines.append(f"  - {h_ord} house ({sign_on_cusp} on cusp): {', '.join(occupants)}")
        else:
            occupants_lines.append(f"  - {h_ord} house ({sign_on_cusp} on cusp): EMPTY (no planets)")

    # Build ruler lines with explicit "DOES NOT live in this house" warning where relevant
    def ordinal(n):
        return {1:"1st",2:"2nd",3:"3rd",4:"4th",5:"5th",6:"6th",7:"7th",8:"8th",9:"9th",10:"10th",11:"11th",12:"12th"}.get(n, f"{n}th")

    def describe_ruler(ruler_name, h_ord_label):
        """Return a string describing where this ruler sits and whether it's in its own house."""
        ruler_data = pd.get(ruler_name, {})
        ruler_sign = ruler_data.get("sign", "?")
        ruler_house = ruler_data.get("house", "?")
        ruler_pos = ruler_data.get("position", "?")
        ruler_house_num = house_num_map.get(ruler_house, 0)
        if h_ord_label and ruler_house == h_ord_label:
            return f"{ruler_name} sits in {ruler_sign} at {ruler_pos}° in the {ruler_house} house (ruler IS in its own house here)"
        elif h_ord_label:
            return f"{ruler_name} sits in {ruler_sign} at {ruler_pos}° in the {ruler_house} house (ruler is NOT in the {h_ord_label} house, it is in the {ruler_house})"
        else:
            return f"{ruler_name} sits in {ruler_sign} at {ruler_pos}° in the {ruler_house} house"

    ruler_lines = []
    for h in [1, 2, 3, 6, 10, 11]:
        h_ord = ordinal(h)
        trad_ruler = hr[h]["ruler"]
        modern_ruler = hr[h].get("modern_ruler")
        trad_desc = describe_ruler(trad_ruler, h_ord)
        line = f"  - {h_ord} house ({hr[h]['sign']} on cusp): TRADITIONAL ruler is {trad_ruler}. {trad_desc}"
        if modern_ruler:
            modern_desc = describe_ruler(modern_ruler, h_ord)
            line += f"\n      Modern co-ruler is {modern_ruler}. {modern_desc}"
        ruler_lines.append(line)

    # Build MC ruler block separately - always included, with both trad and modern
    mc_r = chart.get("mc_rulers", {})
    mc_trad = mc_r.get("ruler")
    mc_modern = mc_r.get("modern_ruler")
    mc_ruler_lines = []
    if mc_trad:
        mc_ruler_lines.append(f"  - MC sign is {mc_r['sign']}. TRADITIONAL ruler of the MC is {mc_trad}. {describe_ruler(mc_trad, None)}")
    if mc_modern:
        mc_ruler_lines.append(f"      Modern co-ruler of the MC is {mc_modern}. {describe_ruler(mc_modern, None)}")

    # Build categorized aspects for key chart points (rulers, ASC, MC, Sun, Moon)
    # so the AI knows which aspects belong to which interpretive layer.
    # Include traditional rulers PRIMARILY, then modern rulers, then MC rulers.
    key_points = ["Sun", "Moon", "Ascendant", "Medium Coeli", "Imum Coeli"]
    for h in [1, 2, 6, 10, 11]:
        trad = hr[h]["ruler"]
        if trad and trad not in key_points:
            key_points.append(trad)
        mod = hr[h].get("modern_ruler")
        if mod and mod not in key_points:
            key_points.append(mod)
    # Also add MC rulers (the MC ruler is critical for vocational identity)
    if mc_trad and mc_trad not in key_points:
        key_points.append(mc_trad)
    if mc_modern and mc_modern not in key_points:
        key_points.append(mc_modern)

    relevant_aspects = []
    for asp in aspects[:30]:
        if asp["p1"] in key_points or asp["p2"] in key_points or asp["p1"] in pd or asp["p2"] in pd:
            relevant_aspects.append(asp)

    aspect_lines = [f"  - {x['p1']} {x['aspect']} {x['p2']} (orb: {x['orb']}°)" for x in relevant_aspects[:20]]

    # Build a per-planet aspect summary - what each KEY planet aspects
    key_planet_aspects = {}
    for asp in aspects:
        for side in [asp["p1"], asp["p2"]]:
            if side in key_points:
                other = asp["p2"] if side == asp["p1"] else asp["p1"]
                key_planet_aspects.setdefault(side, []).append(f"{asp['aspect']} {other} ({asp['orb']}°)")

    key_aspects_summary_lines = []
    for kp in key_points:
        if kp in key_planet_aspects:
            top_aspects = key_planet_aspects[kp][:5]
            key_aspects_summary_lines.append(f"  - {kp}: {'; '.join(top_aspects)}")

    chart_data = f"""BIRTH DETAILS: {chart['name']}, {birth_info['date']}, {birth_info['time']}, {birth_info['city']}, {birth_info['country']}
House System: Whole Sign

PLANETS BY POSITION:
{chr(10).join(planet_lines)}

PLANETS IN EACH HOUSE (this is the AUTHORITATIVE list of which planets occupy each house, use ONLY this for "planets in the X house" statements):
{chr(10).join(occupants_lines)}

ANGLES:
  - ASC: {a['ASC']['sign']} {a['ASC']['position']}°
  - MC: {a['MC']['sign']} {a['MC']['position']}° (sits in Whole Sign house {a['MC']['ws_house']})
  - IC: {a['IC']['sign']} {a['IC']['position']}° (sits in Whole Sign house {a['IC']['ws_house']})

HOUSE RULERS (the ruler is the planet that GOVERNS the house's sign, not the planet INSIDE the house. Use TRADITIONAL rulers as the PRIMARY interpretive layer; modern co-rulers add nuance but never override the traditional reading):
{chr(10).join(ruler_lines)}

MC RULERS (the MC sign rulers are critical for vocational identity. Reference the MC ruler placement and aspects when discussing career):
{chr(10).join(mc_ruler_lines) if mc_ruler_lines else "  - (no MC rulers identified)"}

PART OF FORTUNE: {pof['sign']} in house {pof['house']}

KEY ASPECTS BY PLANET (the most important aspects for each key point in this chart):
{chr(10).join(key_aspects_summary_lines)}

KEY ASPECTS (tightest first, all major aspects):
{chr(10).join(aspect_lines)}"""

    if preview_only:
        return f"""You are a professional astrologer writing a single opening paragraph called "Soul's Signature" for a premium birth chart report. Second person. Match the language register precisely to this person's elemental signature.

{language_guidance}

{chart_data}

Write ONLY this one section. EXACTLY 4-5 sentences. Capture the essence of who this person is at their core, the quality they carry into every room. Weave together Sun, Moon, ASC and the 2-3 tightest aspects. Make it feel like the most accurate thing anyone has ever said about them. Output only the paragraph content, no heading, no preamble. Honour the language register above without naming it explicitly."""

    return f"""You are a professional astrologer writing a premium, deeply personal Life Purpose, Career & Business Blueprint Report. Second person. No jargon, only meaning. Every sentence must feel specific to this person. Be rich and detailed, this is a paid premium report.

CRITICAL, ADAPT LANGUAGE TO THIS CHART:
{language_guidance}

The above language register applies throughout the ENTIRE report. Even when discussing practical career advice, frame it in language that matches this person's elemental signature. Two charts with the same placements should receive the same astrological insights but in noticeably different prose registers.

{chart_data}

Write the report using EXACTLY these eight sections with ## headers. Go deep. Use ### sub-headings as specified below. Every sub-section must have at least 1 full paragraph. When interpreting any house, always cover BOTH the sign on the cusp (what colour of energy flows through it) AND the planets in it (what is being expressed there).

## Your Soul's Signature
4-5 sentences. A powerful, poetic portrait capturing the essence of who this person is at their core. Weave together Sun, Moon, ASC and the 2-3 tightest aspects.

## Your IC, Where You Come From
3 paragraphs. IC sign and Whole Sign house placement, emotional foundation, early environment. The IC-to-MC axis as the defining arc of life. Include planets conjunct IC or MC.

## Your Life Purpose
4 paragraphs. North and South Node, signs, houses, what axis reveals about soul's direction. Include aspects to nodes. What to move toward, what pattern to release.

## Your Career Path & Calling
Use these EXACT ### sub-headings, one paragraph each:

### The 10th House: Your Vocation
Sign on the 10th house cusp (the energy of their calling) AND any planets in the 10th house (what is being expressed publicly). Cover both fully.

### The 6th House: Your Daily Work
Sign on the 6th house cusp AND any planets there. What daily work environment and rhythm suits them.

### The 2nd House: Money and Values
Sign on the 2nd house cusp AND any planets there. Their relationship with money, values, and material security.

### The Career Ruler: Where Your Career Energy Flows
The ruler of the 10th house sign, where it sits, its sign and house placement, its aspects. What this reveals about where career energy actually plays out.

### The MC: Your Public Reputation
MC sign AND the Whole Sign house it falls into. What they will become publicly known for.

## Careers That Fit Your Chart
List 5-6 specific real-world career examples with 2-3 sentences explaining why each fits this chart. Use this EXACT format for each: start the career name as a ### sub-heading on its own line, then a 2-3 sentence explanation paragraph below referencing specific placements that make it a fit.

### [Career name 1]
2-3 sentence explanation referencing chart placements.

### [Career name 2]
2-3 sentence explanation referencing chart placements.

(Continue for 5-6 careers total. Make them genuinely different, not variations on the same theme.)

## Your Unique Gifts
Identify THREE distinct gifts from the chart, drawing on different sources: benefic aspects to personal planets (Sun, Moon, Mercury, Venus, Mars), the 9th house, Chiron as wound-become-gift, the Part of Fortune, Jupiter placement and aspects, or trines/sextiles in the chart. Each gift must be a different theme, not three angles on the same thing.

Use this EXACT format for each:

### Gift One: [short title naming the gift, 3-6 words]
2 paragraphs. The first paragraph names the gift specifically and ties it to the chart placement(s) it comes from, including at least one aspect with its exact orb. The second paragraph describes how this gift shows up in their life and how it serves them and others.

### Gift Two: [short title naming the gift, 3-6 words]
Same two-paragraph structure: name the gift with chart-specific grounding, then describe how it shows up.

### Gift Three: [short title naming the gift, 3-6 words]
Same two-paragraph structure: name the gift with chart-specific grounding, then describe how it shows up.

## Your Greatest Challenges
Identify THREE distinct challenges from the chart, drawing on different sources: tight difficult aspects under 5° orb, Saturn placement and aspects, South Node patterns, 12th house planets, or hard aspects to the Sun/Moon/ASC. Each challenge must be a different theme, not three angles on the same thing.

Use this EXACT format for each:

### Challenge One: [short title naming the pattern, 3-6 words]
First paragraph: Name the challenge with compassion and specificity, tied to a placement. NEVER say there is "something wrong" with this person. NEVER frame as a fixed limitation. Frame as a recurring pattern or growth-edge.
Second paragraph: Reveal the gift inside it, every difficult placement contains a strength being forged. Show how this tension, once met consciously, becomes one of their most valuable qualities. End with 1-2 concrete reframes or practices that help them work WITH this energy.

### Challenge Two: [short title naming the pattern, 3-6 words]
Same two-paragraph structure: name the pattern with compassion, then reveal the gift and offer concrete reframes.

### Challenge Three: [short title naming the pattern, 3-6 words]
Same two-paragraph structure: name the pattern with compassion, then reveal the gift and offer concrete reframes.

CRITICAL TONE RULES for this section:
- Warm, honest, never pitying. Never doom-laden.
- The challenges are real AND they are bigger than them.
- Empower them as the creator of their own experience.
- The reader should close this section feeling more powerful, not less.
- Each challenge must reference a specific placement or aspect with its orb.

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

Make these actions specific and executable, not "reflect on your purpose" but "open a Google Doc and write for 15 minutes about X" or "post one piece of content this week about Y" or "have a conversation with Z about W". Tie each step to the signatures in their chart. Range across: something internal/reflective, something creative/expressive, something external/relational.

FORMATTING RULES, FOLLOW STRICTLY:
- Start directly with "## Your Soul's Signature". No title like "# Report For [Name]".
- Do NOT use horizontal rules (no ---, no ***, no ___).
- Do NOT use **bold text** as a sub-heading. Use ### instead.
- Use ## only for the eight main section headings. Use ### for sub-sections exactly as specified above.
- Regular prose paragraphs only. No numbered lists in running prose ("(1) X, (2) Y"), use ### sub-headings instead.
- For career examples, use ### sub-headings with the career name (not bold text in running prose).

PUNCTUATION RULES, FOLLOW STRICTLY:
- DO NOT use em-dashes (,) anywhere in the report. They make prose feel AI-generated.
- DO NOT use en-dashes (–) for parentheticals.
- Instead, use commas, full stops, semicolons, colons, or parentheses depending on what the sentence needs.
- For a strong pause that would normally use an em-dash, use a comma or full stop. For a parenthetical aside, use commas or parentheses.
- The only place a hyphen is acceptable is between compound words (e.g. "ten-year-old", "well-meaning").

CONSISTENCY RULES, non-negotiable substance that must be covered the same way every time:
- ALWAYS use Whole Sign houses. Never Placidus, Equal, or Koch.
- The tightest aspects (smallest orb) ALWAYS carry the most interpretive weight.
- ALWAYS state explicitly which Whole Sign house the MC and IC fall in.

CRITICAL: PLANETS-IN-HOUSE vs HOUSE RULER (do not confuse these):
- A planet IS IN a house only when it is listed as occupying that house in the "PLANETS IN EACH HOUSE" section above. This is the ONLY authoritative source.
- The house RULER is a different concept: it is the planet that governs the sign on the house cusp. The ruler may or may not be physically located in that house.
- NEVER say a planet is "in" a house unless the data above confirms it. Saying "Saturn in your 2nd house" when Saturn is actually in the 3rd is a serious factual error.
- When discussing a house, use this pattern: "Your [Nth] house in [sign] contains [planets in house]. The ruler, [ruler], sits in [ruler's actual sign and house], which means..." Always distinguish between what is IN the house vs what RULES the house.
- If a house is EMPTY, say so and read the house from its ruler's placement and aspects.

CRITICAL: TRADITIONAL VS MODERN RULERSHIP (Lunabylena house style):
- Use TRADITIONAL rulers as the PRIMARY interpretive layer for every house. The traditional ruler carries the main interpretation.
- The traditional rulers are: Aries → Mars, Taurus → Venus, Gemini → Mercury, Cancer → Moon, Leo → Sun, Virgo → Mercury, Libra → Venus, Scorpio → Mars, Sagittarius → Jupiter, Capricorn → Saturn, Aquarius → Saturn, Pisces → Jupiter.
- Modern rulers (Aquarius → Uranus, Pisces → Neptune, Scorpio → Pluto) are ALSO meaningful and add nuance, but they NEVER replace the traditional reading. Mention the modern co-ruler as a secondary layer where it adds genuine insight (especially for outer-planet themes like awakening, dissolution, or transformation).
- For an Aquarius-ruled house: lead with Saturn's placement and aspects, then add what Uranus brings as a co-ruler. Same logic for Pisces (Jupiter primary, Neptune secondary) and Scorpio (Mars primary, Pluto secondary).

ASPECT COVERAGE (must be present throughout):
- Every house discussed in the Career Path section MUST reference at least one major aspect to either the planets IN that house OR to its TRADITIONAL ruler. Use the "KEY ASPECTS BY PLANET" data to find them.
- The MC RULER must always be discussed with at least one aspect by exact orb. The MC ruler is the planet that governs the MC sign, its placement and aspects describe how this person's vocational identity actually expresses. Reference the traditional MC ruler primarily; bring in the modern MC ruler if the chart has tight or notable aspects involving it.
- Every gift and challenge MUST cite at least one specific aspect by name with its exact orb (e.g. "Venus square Neptune, 0.44°").
- The Soul's Signature MUST reference the 2-3 tightest aspects in the chart.
- The Message From Your Chart MUST reference the single most exact aspect.
- ALWAYS state the TRADITIONAL ruler of the Ascendant sign, where it sits, and at least one aspect to it. If the ASC is in Aquarius, Pisces, or Scorpio, also discuss the modern co-ruler briefly.
- If a person has a stellium (3+ planets in one sign or one Whole Sign house), ALWAYS name it.
- The "Your First Three Steps" section must have one internal/reflective action, one creative/expressive action, and one external/relational action.

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
    # Em-dash (U+2014) with optional surrounding spaces -> comma + space
    text = re.sub(r'\s*—\s*', ', ', text)
    # En-dash (U+2013) with optional surrounding spaces -> comma + space
    text = re.sub(r'\s*–\s*', ', ', text)
    # Also catch the HTML entities in case they appear
    text = text.replace('&mdash;', ', ').replace('&ndash;', ', ')
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
    """v2 design - bold, editorial, on-brand."""
    return f"""<!DOCTYPE html>
<html><head>
<meta charset="UTF-8">
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,400;0,500;0,700;1,400;1,500;1,700&family=Mrs+Saint+Delafield&display=swap" rel="stylesheet">
</head>
<body style="margin:0;padding:0;background:#EFEBEA;font-family:'Playfair Display',Georgia,serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#EFEBEA;padding:50px 20px;">
<tr><td align="center">
  <table width="580" cellpadding="0" cellspacing="0" style="max-width:580px;">

    <tr><td style="text-align:center;padding-bottom:30px;">
      <span style="color:#AA3157;font-size:18px;letter-spacing:0.4em;">✦ ✦ ✦</span>
    </td></tr>

    <tr><td style="text-align:center;padding-bottom:36px;">
      <div style="font-family:Impact,'Arial Narrow Bold',sans-serif;font-size:42px;letter-spacing:0.02em;line-height:0.95;color:#AA3157;text-transform:uppercase;">
        CELESTIAL
      </div>
      <div style="font-family:Impact,'Arial Narrow Bold',sans-serif;font-size:42px;letter-spacing:0.02em;line-height:0.95;color:#C04C2D;text-transform:uppercase;">
        BLUEPRINT
      </div>
    </td></tr>

    <tr><td style="text-align:center;padding-bottom:36px;">
      <div style="display:inline-block;width:80px;height:1px;background:#AA3157;"></div>
    </td></tr>

    <tr><td style="font-family:'Playfair Display',Georgia,serif;font-size:18px;line-height:1.85;color:#1E1E1E;text-align:left;padding:0 20px;">
      <p style="margin:0 0 22px;font-family:'Playfair Display',Georgia,serif;">Dear {name},</p>

      <p style="margin:0 0 22px;font-family:'Playfair Display',Georgia,serif;">Thank you so much for ordering your Celestial Blueprint. Your complete Life Purpose, Career & Business Blueprint report is attached as a PDF.</p>

      <p style="margin:0 0 22px;font-family:'Playfair Display',Georgia,serif;">Take a moment to read it somewhere quiet where you can let it land. My hope is that it reflects something true about you, and perhaps puts words to things you have always sensed but never quite named.</p>

      <p style="margin:0 0 22px;font-family:'Playfair Display',Georgia,serif;">I am so grateful for your trust and support. If the reading resonates, I would love to hear from you.</p>

      <p style="margin:0 0 4px;font-family:'Playfair Display',Georgia,serif;">With warmth,</p>
      <p style="margin:0 0 0;font-family:'Mrs Saint Delafield',cursive;font-size:32px;color:#AA3157;line-height:1;">Lena</p>
    </td></tr>

    <tr><td style="padding-top:50px;text-align:center;">
      <div style="display:inline-block;width:60px;height:1px;background:#AA3157;margin-bottom:18px;"></div>
      <div style="color:#AA3157;font-family:Impact,'Arial Narrow Bold',sans-serif;font-size:13px;letter-spacing:0.3em;text-transform:uppercase;">
        ✦ Lunabylena.com ✦
      </div>
      <div style="font-family:'Playfair Display',Georgia,serif;font-style:italic;font-size:12px;color:#8A7575;margin-top:8px;">
        Whole Sign houses · Swiss Ephemeris
      </div>
    </td></tr>

  </table>
</td></tr>
</table>
</body></html>"""

def build_pdf_html(name, report_text, birth_info, chart):
    """v2 design - bold editorial PDF, matches website."""
    report_body = markdown_to_html(report_text)
    name_possessive = "&#39;" if name.endswith("s") else "&#39;s"
    city_upper = birth_info["city"].upper()
    country_upper = birth_info["country"].upper()

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

    def make_cell(label, value, house):
        return f'<td><div class="cell-label">{label}</div><div class="cell-value">{value}</div><div class="cell-house">{house}</div></td>'

    row1 = "".join(make_cell(*c) for c in cells[:5])
    row2 = "".join(make_cell(*c) for c in cells[5:])
    cells_html = f"<tr>{row1}</tr><tr>{row2}</tr>"

    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<style>
  @import url('https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,400;0,500;0,700;1,400;1,500;1,700&family=Mrs+Saint+Delafield&display=swap');
  @page {{ size: A4; margin: 18mm 18mm; background: #EFEBEA; }}

  * {{ box-sizing: border-box; }}

  html, body {{
    margin: 0;
    padding: 0;
    background: #EFEBEA;
    font-family: 'Playfair Display', Georgia, serif;
    color: #1E1E1E;
  }}

  .page {{ background: #EFEBEA; }}

  /* ========== COVER PAGE ========== */
  .cover {{
    text-align: center;
    padding: 50px 0 30px;
    page-break-after: always;
  }}

  .cover .stars-row {{
    margin-bottom: 32px;
    color: #AA3157;
    font-size: 16px;
    letter-spacing: 0.4em;
  }}

  .cover .brand {{
    font-family: Impact, 'Arial Narrow Bold', sans-serif;
    font-size: 76px;
    line-height: 0.92;
    letter-spacing: 0.02em;
    text-transform: uppercase;
    margin: 0;
  }}

  .cover .brand .line1 {{ color: #AA3157; display: block; }}
  .cover .brand .line2 {{ color: #C04C2D; display: block; }}

  .cover .tagline {{
    font-family: 'Playfair Display', serif;
    font-style: italic;
    font-size: 14px;
    color: #3A3030;
    margin: 22px 0 0;
    letter-spacing: 0.02em;
  }}

  .cover-divider {{
    width: 80px;
    height: 1px;
    background: #AA3157;
    margin: 50px auto;
  }}

  .cover .eyebrow {{
    display: inline-block;
    font-family: Impact, 'Arial Narrow Bold', sans-serif;
    font-size: 11px;
    letter-spacing: 0.3em;
    text-transform: uppercase;
    color: #EFEBEA;
    background: #AA3157;
    padding: 6px 18px;
    margin-bottom: 28px;
  }}

  .cover .report-name {{
    font-family: 'Playfair Display', serif;
    font-weight: 700;
    font-size: 52px;
    color: #1E1E1E;
    margin: 0 0 16px;
    line-height: 1.05;
  }}

  .cover .report-name .italic {{
    font-style: italic;
    color: #AA3157;
    font-weight: 700;
  }}

  .cover .meta {{
    font-family: Impact, 'Arial Narrow Bold', sans-serif;
    font-size: 10px;
    letter-spacing: 0.22em;
    text-transform: uppercase;
    color: #7E4A92;
    margin: 18px 0 0;
  }}

  /* ========== CHART STRIP ========== */
  .chart-strip-heading {{
    font-family: Impact, 'Arial Narrow Bold', sans-serif;
    font-size: 11px;
    letter-spacing: 0.3em;
    text-transform: uppercase;
    color: #AA3157;
    text-align: center;
    margin: 0 0 18px;
  }}

  .chart-on-cover {{
    margin-top: 32px;
  }}

  .chart-table {{
    width: 100%;
    border-collapse: collapse;
    border: 2px solid #1E1E1E;
    margin: 0;
    table-layout: fixed;
  }}

  .chart-table tr {{
    height: 56px;
  }}

  .chart-table td {{
    background: #EFEBEA;
    padding: 6px 4px;
    text-align: center;
    border: 1px solid #1E1E1E;
    width: 20%;
    vertical-align: middle;
  }}

  .cell-label {{
    font-family: Impact, 'Arial Narrow Bold', sans-serif;
    font-size: 8px;
    letter-spacing: 0.18em;
    text-transform: uppercase;
    color: #7E4A92;
    margin-bottom: 3px;
  }}

  .cell-value {{
    font-family: 'Playfair Display', serif;
    font-weight: 500;
    font-size: 13px;
    color: #1E1E1E;
  }}

  .cell-house {{
    font-family: Impact, 'Arial Narrow Bold', sans-serif;
    font-size: 8px;
    color: #8A7575;
    margin-top: 2px;
    letter-spacing: 0.1em;
  }}

  /* ========== REPORT BODY ========== */
  .report h2 {{
    font-family: Impact, 'Arial Narrow Bold', sans-serif;
    font-size: 24px;
    letter-spacing: 0.02em;
    text-transform: uppercase;
    color: #AA3157;
    margin: 32px 0 14px;
    padding-bottom: 8px;
    border-bottom: 2px solid #1E1E1E;
    line-height: 1.05;
    page-break-after: avoid;
  }}

  .report h3 {{
    font-family: 'Playfair Display', serif;
    font-weight: 700;
    font-style: italic;
    font-size: 14px;
    color: #1E1E1E;
    margin: 20px 0 8px;
    page-break-after: avoid;
  }}

  .report h3::before {{
    content: '✦  ';
    color: #AA3157;
    font-style: normal;
    font-weight: 400;
    font-size: 11px;
  }}

  .report p {{
    font-family: 'Playfair Display', Georgia, serif;
    font-size: 12px;
    line-height: 1.75;
    color: #3A3030;
    margin: 0 0 12px;
    text-align: left;
    orphans: 3;
    widows: 3;
  }}

  .report p strong {{
    font-weight: 700;
    color: #1E1E1E;
  }}

  /* ========== SPECIAL SECTIONS ========== */
  .message-callout {{
    margin: 36px 0 14px;
    padding: 22px 26px;
    background: #FFE3EC;
    border: 2px solid #1E1E1E;
    position: relative;
    page-break-inside: avoid;
    box-decoration-break: clone;
    -webkit-box-decoration-break: clone;
  }}

  .message-callout h2 {{
    margin: 0 0 12px;
    padding: 0;
    border: none;
    color: #AA3157;
    font-size: 20px;
  }}

  .message-callout p {{
    font-style: italic;
    color: #1E1E1E;
    font-size: 12.5px;
    line-height: 1.85;
  }}

  .business-section {{
    margin: 36px 0 14px;
    padding: 22px 26px;
    background: #FFFFFF;
    border: 2px solid #1E1E1E;
    box-decoration-break: clone;
    -webkit-box-decoration-break: clone;
  }}

  .business-section h2 {{
    margin-top: 0;
    color: #C04C2D;
  }}

  .steps-section {{
    margin: 36px 0 14px;
    padding: 24px 28px;
    background: #AA3157;
    color: #EFEBEA;
    border: 2px solid #1E1E1E;
    box-decoration-break: clone;
    -webkit-box-decoration-break: clone;
  }}

  .steps-section h2 {{
    margin-top: 0;
    color: #EFEBEA;
    border-bottom: 2px solid #EFEBEA;
  }}

  .steps-section h3 {{
    color: #EFEBEA;
    font-style: normal;
    font-family: Impact, 'Arial Narrow Bold', sans-serif;
    font-size: 11px;
    letter-spacing: 0.2em;
    text-transform: uppercase;
    margin-top: 18px;
  }}

  .steps-section h3::before {{
    color: #EFEBEA;
  }}

  .steps-section p {{
    color: #EFEBEA;
    font-style: italic;
  }}

  /* ========== FOOTER ========== */
  .footer {{
    margin-top: 50px;
    padding-top: 20px;
    border-top: 1px solid #AA3157;
    text-align: center;
  }}

  .footer-label {{
    font-family: Impact, 'Arial Narrow Bold', sans-serif;
    font-size: 10px;
    letter-spacing: 0.3em;
    text-transform: uppercase;
    color: #AA3157;
  }}

  .footer-note {{
    font-family: 'Playfair Display', serif;
    font-style: italic;
    font-size: 9px;
    color: #8A7575;
    margin-top: 4px;
  }}
</style>
</head>
<body>
<div class="page">

  <div class="cover">
    <div class="stars-row">✦ ✦ ✦</div>
    <h1 class="brand">
      <span class="line1">CELESTIAL</span>
      <span class="line2">BLUEPRINT</span>
    </h1>
    <p class="tagline">Life Purpose · Career · Personal Brand</p>

    <div class="cover-divider"></div>

    <span class="eyebrow">The Purpose Blueprint</span>
    <h2 class="report-name">{name}{name_possessive} <span class="italic">Purpose Blueprint</span></h2>
    <p class="meta">{birth_info['date']} · {birth_info['time']} · {city_upper}, {country_upper}</p>

    <div class="chart-on-cover">
      <p class="chart-strip-heading">Your Chart at a Glance</p>
      <table class="chart-table">
        {cells_html}
      </table>
    </div>
  </div>

  <div class="report">
    {report_body}
  </div>

  <div class="footer">
    <div class="footer-label">✦ Lunabylena.com ✦</div>
    <div class="footer-note">Whole Sign houses · Swiss Ephemeris</div>
  </div>

</div>
</body></html>"""


def send_report_email(to_email, to_name, email_body_html, pdf_bytes, subject=None, filename=None):
    """Send email via Resend API with short personal body + PDF attachment."""
    api_key = os.environ.get("RESEND_API_KEY", "")
    if not api_key:
        print("WARNING: No RESEND_API_KEY set")
        return False

    payload = {
        "from": "Celestial Blueprint <hello@lunabylena.com>",
        "to": [to_email],
        "subject": subject or f"Your Celestial Blueprint ✦ {to_name}",
        "html": email_body_html,
    }

    if pdf_bytes:
        pdf_b64 = base64.b64encode(pdf_bytes).decode()
        payload["attachments"] = [{
            "filename": filename or f"{to_name}-celestial-blueprint.pdf",
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


def add_to_kit(name, email, tag_name="purpose-blueprint"):
    """Add a subscriber to Kit using V4 API with X-Kit-Api-Key header."""
    import requests as req

    api_key = os.environ.get("KIT_API_KEY")
    if not api_key:
        print("Kit: KIT_API_KEY not set, skipping")
        return False

    first_name = name.split()[0] if name else ""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Kit-Api-Key": api_key,
    }
    base = "https://api.kit.com/v4"

    try:
        # Step 1: Create or update subscriber
        sub_resp = req.post(
            f"{base}/subscribers",
            json={"email_address": email, "first_name": first_name},
            headers=headers,
            timeout=10
        )
        sub_data = sub_resp.json()

        if sub_resp.status_code not in (200, 201):
            print(f"Kit: failed to create subscriber: {sub_resp.status_code} {sub_data}")
            return False

        subscriber_id = sub_data.get("subscriber", {}).get("id")
        if not subscriber_id:
            print(f"Kit: no subscriber id in response: {sub_data}")
            return False

        # Step 2: Get or create the tag
        tags_resp = req.get(f"{base}/tags", headers=headers, timeout=10)
        tag_id = None
        for tag in tags_resp.json().get("tags", []):
            if tag.get("name") == tag_name:
                tag_id = tag["id"]
                break

        if not tag_id:
            create_resp = req.post(
                f"{base}/tags",
                json={"name": tag_name},
                headers=headers,
                timeout=10
            )
            tag_id = create_resp.json().get("tag", {}).get("id")

        if not tag_id:
            print("Kit: subscriber added but could not get/create tag")
            return True

        # Step 3: Tag the subscriber
        tag_resp = req.post(
            f"{base}/tags/{tag_id}/subscribers/{subscriber_id}",
            headers=headers,
            timeout=10
        )

        if tag_resp.status_code in (200, 201):
            print(f"Kit: added {email} with tag {tag_name} (subscriber {subscriber_id})")
        else:
            print(f"Kit: subscriber added but tagging failed: {tag_resp.status_code} {tag_resp.text[:200]}")
        return True

    except Exception as e:
        print(f"Kit: error adding subscriber: {e}")
        return False

def log_customer(name, email, marketing_opt_in, date, city, country, tag_name="purpose-blueprint"):
    """Log customer to CSV (always) and push to Kit if they opted in."""
    import csv
    from datetime import datetime

    # Always log to CSV as a backup record
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

    # Push to Kit only if they opted in
    if marketing_opt_in:
        add_to_kit(name=name, email=email, tag_name=tag_name)


@app.route("/")
def index():
    return render_template("home.html")


@app.route("/purpose")
def purpose():
    return render_template("index.html",
        auto_generate=False,
        chart_data="null",
        meta_data="null")


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

    # Stream the preview only — full PDF is sent after payment
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


@app.route("/create-checkout-session", methods=["POST"])
def create_checkout_session():
    """Create a Stripe checkout session and return the URL."""
    import stripe as stripe_lib
    stripe_lib.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
    data = request.json

    # Validate required fields before charging
    name = data.get("name", "").strip()
    email = data.get("email", "").strip()
    if not name or not email or "@" not in email:
        return jsonify({"error": "Please provide your name and a valid email address."}), 400
    if not data.get("date") or not data.get("time"):
        return jsonify({"error": "Please provide your birth date and time."}), 400
    if not data.get("lat") or not data.get("lng"):
        return jsonify({"error": "Please select a city from the dropdown."}), 400

    try:
        # Store birth data in Stripe metadata so we can use it after payment
        session = stripe_lib.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "eur",
                    "unit_amount": PRICE_EUR,
                    "product_data": {
                        "name": "The Purpose Blueprint",
                        "description": "Your personalised astrology report — Life Purpose, Career & Personal Brand",
                    },
                },
                "quantity": 1,
            }],
            mode="payment",
            customer_email=email,
            success_url=f"{request.host_url}purpose/payment-success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{request.host_url}purpose?cancelled=true",
            metadata={
                "name": name,
                "email": email,
                "date": data.get("date", ""),
                "time": data.get("time", ""),
                "city": data.get("city", ""),
                "country": data.get("country", ""),
                "lat": str(data.get("lat", "")),
                "lng": str(data.get("lng", "")),
                "tz": data.get("tz", "UTC"),
                "marketingOptIn": "true" if data.get("marketingOptIn") else "false",
            }
        )
        return jsonify({"url": session.url})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/purpose/payment-success")
def payment_success():
    """After Stripe payment, verify session then render the page.
    The frontend calls /generate-after-payment to stream the report."""
    session_id = request.args.get("session_id")
    if not session_id:
        return render_template("index.html",
            auto_generate=False, chart_data="null", meta_data="null")

    try:
        import stripe as stripe_lib
        stripe_lib.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
        print(f"[payment-success] Retrieving session: {session_id[:20]}...")
        session = stripe_lib.checkout.Session.retrieve(session_id)
        print(f"[payment-success] Payment status: {session.payment_status}")
        if session.payment_status != "paid":
            return render_template("index.html",
                auto_generate=False, chart_data="null", meta_data="null")

        meta = session.metadata.to_dict()
        print(f"[payment-success] Verified — rendering thank you page for {meta.get('email')}")
        return render_template("thank_you.html",
            session_id=session_id,
            name=meta.get("name", ""),
            email=meta.get("email", ""))

    except Exception as e:
        print(f"Payment success error: {e}")
        import traceback; traceback.print_exc()
        return render_template("index.html",
            auto_generate=False, chart_data="null", meta_data="null")


@app.route("/generate-after-payment", methods=["POST"])
def generate_after_payment():
    """Stream report for a verified paid session. Fetches birth data
    directly from Stripe metadata so no in-memory state is needed."""
    import stripe as stripe_lib
    stripe_lib.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
    data = request.json
    session_id = data.get("session_id", "")

    print(f"[generate-after-payment] Called with session_id={session_id[:20] if session_id else 'NONE'}")

    try:
        session = stripe_lib.checkout.Session.retrieve(session_id)
    except Exception as e:
        print(f"[generate-after-payment] Stripe retrieve failed: {type(e).__name__}: {e}")
        return jsonify({"error": f"Could not verify payment session: {type(e).__name__}: {e}"}), 400

    if session.payment_status != "paid":
        print(f"[generate-after-payment] Payment not complete: {session.payment_status}")
        return jsonify({"error": "Payment not complete."}), 400

    meta = session.metadata.to_dict()
    print(f"[generate-after-payment] Meta keys: {list(meta.keys())}")

    payload = {
        "name": meta.get("name", ""),
        "email": meta.get("email", ""),
        "date": meta.get("date", ""),
        "time": meta.get("time", ""),
        "city": meta.get("city", ""),
        "country": meta.get("country", ""),
        "lat": meta.get("lat", "0"),
        "lng": meta.get("lng", "0"),
        "tz": meta.get("tz", "UTC"),
        "marketingOptIn": meta.get("marketingOptIn") == "true",
    }
    print(f"[generate-after-payment] Session found, email={payload.get('email')}")
    name = payload["name"] or "the person"
    email = payload["email"]
    date_str = payload["date"]
    time_str = payload["time"]
    city = payload["city"]
    country = payload["country"]
    tz_str = payload["tz"]
    marketing_opt_in = payload["marketingOptIn"]

    try:
        lat = float(payload["lat"])
        lng = float(payload["lng"])
        year, month, day = [int(x) for x in date_str.split("-")]
        hour, minute = [int(x) for x in time_str.split(":")]
    except Exception as e:
        return jsonify({"error": f"Invalid birth data: {e}"}), 400

    try:
        chart = calculate_chart(name, year, month, day, hour, minute, lat, lng, tz_str)
    except Exception as e:
        return jsonify({"error": f"Chart calculation failed: {e}"}), 500

    birth_info = {"date": date_str, "time": time_str, "city": city, "country": country}

    log_customer(name=name, email=email, marketing_opt_in=marketing_opt_in,
                date=date_str, city=city, country=country)

    # Start background full report + email
    thread = threading.Thread(
        target=background_generate_and_send,
        args=(email, chart, birth_info),
        daemon=True
    )
    thread.start()

    # Stream the preview exactly like /generate
    preview_prompt = build_prompt(chart, birth_info, preview_only=True)
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

    def stream():
        # Emit chart + payload so frontend can build the report header correctly
        chart_event = dict(chart)
        yield f"data: {json.dumps({'type': 'chart', 'data': chart_event, 'payload': {'name': name, 'email': email, 'date': date_str, 'time': time_str, 'city': city, 'country': country, 'lat': lat, 'lng': lng, 'tz': tz_str}})}\n\n"
        buffer = ""
        with client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            messages=[{"role": "user", "content": preview_prompt}]
        ) as st:
            for text in st.text_stream:
                buffer += text
                if len(buffer) > 3:
                    flush = buffer[:-3]
                    buffer = buffer[-3:]
                    yield f"data: {json.dumps({'type': 'text', 'content': clean_dashes(flush)})}\n\n"
        if buffer:
            yield f"data: {json.dumps({'type': 'text', 'content': clean_dashes(buffer)})}\n\n"
        yield f"data: {json.dumps({'type': 'done', 'email': email})}\n\n"

    return Response(stream(), mimetype="text/event-stream",
                   headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/stripe-key")
def stripe_key():
    """Return the publishable key for the frontend."""
    return jsonify({"publishable_key": STRIPE_PUBLISHABLE_KEY})


@app.route("/city-search")
def city_search():
    """Proxy Google Places autocomplete — keeps the API key server-side."""
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify({"results": []})
    api_key = os.environ.get("GOOGLE_PLACES_KEY", "")
    if not api_key:
        return jsonify({"results": []})
    try:
        r = requests.get(
            "https://maps.googleapis.com/maps/api/place/autocomplete/json",
            params={"input": q, "types": "(regions)", "language": "en", "key": api_key},
            timeout=5
        )
        preds = r.json().get("predictions", [])
        results = [{
            "place_id": p.get("place_id", ""),
            "main_text": p.get("structured_formatting", {}).get("main_text", ""),
            "secondary_text": p.get("structured_formatting", {}).get("secondary_text", ""),
        } for p in preds]
        return jsonify({"results": results})
    except Exception as e:
        print(f"City search error: {e}")
        return jsonify({"results": []})


@app.route("/city-details")
def city_details():
    """Fetch lat/lng and country for a Google place_id — keeps the API key server-side."""
    place_id = request.args.get("place_id", "")
    if not place_id:
        return jsonify({"error": "No place_id"}), 400
    api_key = os.environ.get("GOOGLE_PLACES_KEY", "")
    try:
        r = requests.get(
            "https://maps.googleapis.com/maps/api/place/details/json",
            params={"place_id": place_id, "fields": "geometry,name,address_components", "key": api_key},
            timeout=5
        )
        result = r.json().get("result", {})
        lat = result.get("geometry", {}).get("location", {}).get("lat")
        lng = result.get("geometry", {}).get("location", {}).get("lng")
        name = result.get("name", "")
        country = next(
            (c.get("long_name", "") for c in result.get("address_components", []) if "country" in c.get("types", [])),
            ""
        )
        return jsonify({"name": name, "country": country, "lat": lat, "lng": lng})
    except Exception as e:
        print(f"City details error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/geocode")
def geocode():
    """Geocode a free-text place name — fallback when user doesn't pick from dropdown."""
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"error": "No query"}), 400
    api_key = os.environ.get("GOOGLE_PLACES_KEY", "")
    try:
        r = requests.get(
            "https://maps.googleapis.com/maps/api/geocode/json",
            params={"address": q, "language": "en", "key": api_key},
            timeout=5
        )
        results = r.json().get("results", [])
        if not results:
            return jsonify({"error": "Not found"}), 404
        res = results[0]
        lat = res["geometry"]["location"]["lat"]
        lng = res["geometry"]["location"]["lng"]
        components = res.get("address_components", [])
        name = next((c["long_name"] for c in components if "locality" in c.get("types", [])
                     or "administrative_area_level_3" in c.get("types", [])), q.split(",")[0].strip())
        country = next((c["long_name"] for c in components if "country" in c.get("types", [])), "")
        return jsonify({"name": name, "country": country, "lat": lat, "lng": lng})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/timezone", methods=["POST"])
def get_timezone():
    """Return the IANA timezone string for a given lat/lng."""
    from timezonefinder import TimezoneFinder
    data = request.json
    try:
        lat = float(data.get("lat", 0))
        lng = float(data.get("lng", 0))
        tz = TimezoneFinder().timezone_at(lat=lat, lng=lng) or "UTC"
    except Exception:
        tz = "UTC"
    return jsonify({"tz": tz})


@app.route("/preview-thank-you")
def preview_thank_you():
    return render_template("thank_you.html",
        session_id=None,
        name="Lena Skogheim",
        email="lena@example.com")


# ─────────────────────────────────────────────
#  LOVE BLUEPRINT — shared helpers
# ─────────────────────────────────────────────

def build_love_prompt(chart, birth_info, preview_only=False):
    pd = chart["planets"]
    a = chart["angles"]
    hr = chart["house_rulers"]
    aspects = chart["aspects"]
    language_guidance = build_language_guidance(
        chart.get("dominant_element", "earth"),
        chart.get("asc_element", "earth"),
        chart.get("element_balance", {"fire":25,"earth":25,"air":25,"water":25})
    )

    planet_lines = [f"  - {n}: {d['sign']}, {d['house']} house, {d['position']}°" for n,d in pd.items()]

    house_occupants = {h: [] for h in range(1, 13)}
    house_num_map = {"1st":1,"2nd":2,"3rd":3,"4th":4,"5th":5,"6th":6,"7th":7,"8th":8,"9th":9,"10th":10,"11th":11,"12th":12}
    for pname, pdata in pd.items():
        h_num = house_num_map.get(pdata["house"])
        if h_num:
            house_occupants[h_num].append(f"{pname} ({pdata['sign']} {pdata['position']}°)")

    ws_signs = ["Aries","Taurus","Gemini","Cancer","Leo","Virgo","Libra","Scorpio","Sagittarius","Capricorn","Aquarius","Pisces"]
    asc_idx_h = ws_signs.index(a["ASC"]["sign"])

    def ordinal(n):
        return {1:"1st",2:"2nd",3:"3rd",4:"4th",5:"5th",6:"6th",7:"7th",8:"8th",9:"9th",10:"10th",11:"11th",12:"12th"}.get(n, f"{n}th")

    occupants_lines = []
    for h in range(1, 13):
        sign_on_cusp = ws_signs[(asc_idx_h + h - 1) % 12]
        occupants = house_occupants[h]
        h_ord = ordinal(h)
        tag = f": {', '.join(occupants)}" if occupants else ": EMPTY (no planets)"
        occupants_lines.append(f"  - {h_ord} house ({sign_on_cusp} on cusp){tag}")

    def describe_ruler(ruler_name, h_ord_label=None):
        rd = pd.get(ruler_name, {})
        rs, rh, rp = rd.get("sign","?"), rd.get("house","?"), rd.get("position","?")
        if h_ord_label and rh == h_ord_label:
            return f"{ruler_name} sits in {rs} at {rp}° in the {rh} house (ruler IS in its own house here)"
        elif h_ord_label:
            return f"{ruler_name} sits in {rs} at {rp}° in the {rh} house (ruler is NOT in the {h_ord_label} house, it is in the {rh})"
        return f"{ruler_name} sits in {rs} at {rp}° in the {rh} house"

    love_ruler_lines = []
    for h in [5, 7, 8]:
        if h not in hr:
            continue
        h_ord = ordinal(h)
        trad = hr[h]["ruler"]
        mod = hr[h].get("modern_ruler")
        line = f"  - {h_ord} house ({hr[h]['sign']} on cusp): TRADITIONAL ruler is {trad}. {describe_ruler(trad, h_ord)}"
        if mod:
            line += f"\n      Modern co-ruler is {mod}. {describe_ruler(mod, h_ord)}"
        love_ruler_lines.append(line)

    # Key points for love
    love_key = ["Venus", "Moon", "Mars", "North Node", "South Node", "Chiron"]
    for h in [5, 7, 8]:
        if h in hr:
            for k in [hr[h]["ruler"], hr[h].get("modern_ruler")]:
                if k and k not in love_key:
                    love_key.append(k)

    love_asp_by_planet = {}
    for asp in aspects:
        for side in [asp["p1"], asp["p2"]]:
            if side in love_key:
                other = asp["p2"] if side == asp["p1"] else asp["p1"]
                love_asp_by_planet.setdefault(side, []).append(f"{asp['aspect']} {other} ({asp['orb']}°)")

    love_summary_lines = []
    for kp in love_key:
        if kp in love_asp_by_planet:
            love_summary_lines.append(f"  - {kp}: {'; '.join(love_asp_by_planet[kp][:5])}")

    aspect_lines = [f"  - {x['p1']} {x['aspect']} {x['p2']} (orb: {x['orb']}°)" for x in aspects[:25]]

    chart_data = f"""BIRTH DETAILS: {chart['name']}, {birth_info['date']}, {birth_info['time']}, {birth_info['city']}, {birth_info['country']}
House System: Whole Sign

PLANETS BY POSITION:
{chr(10).join(planet_lines)}

PLANETS IN EACH HOUSE (AUTHORITATIVE — use ONLY this for "planets in X house" statements):
{chr(10).join(occupants_lines)}

ANGLES:
  - ASC: {a['ASC']['sign']} {a['ASC']['position']}°
  - MC: {a['MC']['sign']} {a['MC']['position']}° (Whole Sign house {a['MC']['ws_house']})
  - IC: {a['IC']['sign']} {a['IC']['position']}° (Whole Sign house {a['IC']['ws_house']})

LOVE-RELEVANT HOUSE RULERS — 5th (romance/pleasure), 7th (partnership), 8th (deep intimacy):
(Use TRADITIONAL rulers as primary; modern co-rulers add nuance but never replace the traditional reading)
{chr(10).join(love_ruler_lines)}

KEY ASPECTS BY PLANET (love-relevant points — use these for aspect citations):
{chr(10).join(love_summary_lines)}

ALL KEY ASPECTS (tightest first):
{chr(10).join(aspect_lines)}"""

    if preview_only:
        return f"""You are writing the opening section, "Your Love Signature", of a premium Love Blueprint natal chart report for {chart['name']}. Second person.

VOICE AND TONE (non-negotiable):
Write in the voice of Luna by Lena: grounded, direct, emotionally intelligent, neutral (never shaming, never enabling), quietly authoritative (you never hype, perform, or rescue), and intimate, like one woman speaking honestly to another. Confronting and safe at the same time. Never use the words: toxic, broken, damaged, blocked, "raise your vibration", "love and light", "heal more", "let go and trust", "just", "simply". Return power to the reader in every sentence.

CRITICAL, ADAPT LANGUAGE TO THIS CHART (sign and dominant element / triplicity):
{language_guidance}

{chart_data}

Write ONLY the "Your Love Signature" section. EXACTLY 4 to 5 sentences. No heading, no preamble.
Open with the single most surprising or paradoxical truth this chart reveals about how {chart['name']} loves. Do NOT open with a gentle observation or a compliment. Open with something so specific to this exact chart that it could not describe anyone else, something that makes her stop and think "how did you know that". Then establish, in the same short paragraph: the core tension or paradox in how she loves, Venus sign and house as the love style, Moon sign and house as the emotional need, and the tightest Venus or Moon aspect named with its exact orb as the throughline. Tie every claim to a specific placement, aspect, and exact orb. Do not use em-dashes. Do not name the elemental register explicitly. Output only the paragraph text."""

    return f"""You are writing a personalised astrology report called The Love Blueprint for {chart['name']}. This report reads their natal chart to reveal how they love, what they need, what they keep attracting, and what their chart says about the relationship patterns that have defined their life. Second person throughout.

VOICE AND TONE (non-negotiable):
Write in the voice of Luna by Lena. That voice is:
- Grounded, direct, emotionally intelligent
- Neutral: never shaming, never enabling
- Quietly authoritative: you do not hype, perform, or rescue
- Intimate: like one woman speaking honestly to another
- Confronting and safe at the same time

Signature language to use naturally throughout:
"This is information." / "This makes sense." / "Without shame." / "Nothing has gone wrong." / "What once kept you safe may now be keeping you stuck."

Language to never use:
"toxic", "broken", "damaged", "blocked", "raise your vibration", "love and light", "heal more", "let go and trust", "just", "simply".

The report should return power to the reader at every turn. Before every paragraph ask: "Does this sentence give her information she can use, or does it create helplessness?" If the latter, rewrite it.

CRITICAL, ADAPT LANGUAGE TO THIS CHART (sign and dominant element / triplicity, earth / water / fire / air):
{language_guidance}

{chart_data}

CORE PRINCIPLE:
Every interpretive statement must be tied to a specific placement, aspect, and orb. No generic love astrology. No statements that could apply to anyone. "Venus in Libra in the 11th house square Neptune at 0.44°" is a completely different story from "Venus in Libra" or "Venus square Neptune". Always use the full picture. Always name aspects with their exact orb. The tightest aspects carry the most interpretive weight: return to them as the throughline of the report.

HOUSE RULERS, MANDATORY IN EVERY HOUSE SECTION:
Every section that discusses a house MUST include:
(a) The sign on the house cusp
(b) Any planets physically occupying that house with their exact positions
(c) The house ruler, the planet governing the cusp sign, named explicitly with its sign, house placement, and at least one major aspect with exact orb.
The ruler is often more revealing than the cusp itself. It is never optional, never mentioned only in passing. It always gets its own interpretive paragraph.

Write the report in the following sections, in this exact order, using ## for each section heading:

## Your Love Signature
Open with the single most surprising or paradoxical truth this chart reveals about how {chart['name']} loves. Do NOT open with a gentle observation or a compliment. Open with something that makes her stop and say "how did you know that". The opener must be specific to this chart and could not work for anyone else's. Then, in 4 to 5 total sentences, establish: the core tension or paradox in how she loves (the most interesting thing), Venus sign and house as the love style, Moon sign and house as the emotional need, and the tightest Venus or Moon aspect by exact name and orb as the throughline. This section sets the emotional tone of the whole report. If it is generic, she will not trust what follows.

## How You Love: Venus
Full Venus interpretation. Cover: what the Venus sign reveals about how she expresses love and what she genuinely values in a relationship (specific to this sign, not generic); what the Venus house placement says about where love shows up in her life and the context in which she falls for people; every significant Venus aspect named explicitly with exact orb and interpreted, not just listed; and the ruler of the Venus house, where it sits, what sign it is in, at least one aspect with exact orb, and what this reveals about how her love nature actually operates in practice. End with the growth edge: what complicates or challenges this Venus, not only its gifts. She should finish this section feeling seen in both her beauty and her pattern.

## What You Desire: Mars
Full Mars interpretation. Cover: what Mars sign reveals about how she pursues and what she is drawn to energetically and physically; what Mars in this specific house reveals about the context and texture of her desire, where it lives and what activates it; every significant Mars aspect named with exact orb and interpreted individually; the ruler of the Mars house (sign, house, and at least one aspect with orb); and what Mars reveals about the gap between what she consciously wants and what her desire nature actually seeks. Handle this with directness and maturity. Desire is normal. Name it clearly without softening or making it abstract.

## What You Need to Feel Safe: The Moon
Full Moon interpretation. Cover: what Moon sign and house reveal about her emotional needs and attachment patterns specifically; what makes her feel genuinely secure versus what destabilises her, named concretely; every significant Moon aspect with exact orb, interpreted individually; the Moon house ruler (sign, house, and at least one aspect with orb); and what she needs from a partner that she may have never directly asked for. IMPORTANT: introduce information not already covered above. If a Venus, Moon tension was explored in How You Love, do not re-explain it here; only reference it if it adds genuinely new meaning in the safety context. Repetition across sections is the single biggest quality failure in this report. Each section must move the story forward.

## The 5th House: Romance, Play, and How You Date
The 5th house governs romance before it becomes commitment: the electricity, the pursuit, the joy, the creative spark of early love. Cover: the sign on the 5th house cusp and what it reveals about her romantic style, what she needs dating to feel like, and what makes chemistry feel real to her; any planets physically in the 5th house with their positions and aspects; the 5th house ruler (sign, house, and at least one major aspect with orb), which tells you where her romantic energy actually flows; what lights her up in early love versus what makes romance feel flat or dead; and what she needs a potential partner to bring before she takes it seriously. This section should feel lighter and more alive than the others. It is about joy in love. Write it with warmth and specificity about pleasure: what this person actually enjoys, not only what she needs.

## The Partner You Seek: 7th House
Cover: the sign on the 7th house cusp and what it reveals about who she consciously seeks in a partner; the projection layer, what qualities she looks for in others that are actually underdeveloped or unclaimed in herself (name this plainly, it is not a criticism, it is information); any planets physically in the 7th house with their positions and aspects; and the 7th house ruler, which is essential and never omitted: its sign, house placement, and at least two major aspects with exact orbs. The ruler is the most important interpretive element of this section; give it a full paragraph. It tells you how her partnership energy actually operates versus how she imagines it does. If the 7th house is empty of planets, spend more time on the ruler, it carries the entire story. End by naming something she is looking for in a partner that she could actually develop in herself.

## The Pattern You Keep Repeating
This is the most confronting section. Write it with full compassion and full directness: do not soften the truth, but do not shame it either. Cover: the South Node in relationship context, the familiar role she defaults to in partnership, what it cost her, and why it once made sense; the specific recurring story in her love life, named plainly ("You consistently do X because Y, and it results in Z"); at least two specific aspects or placements with exact orbs as evidence; why the pattern exists, the fear or wound it is protecting; and what the pattern costs as well as what it is preserving. Never pathologise without acknowledging the intelligence of the protection mechanism. End with one sentence that reframes the pattern as information she can now use rather than a flaw she must fix. Never use "toxic". Never leave her feeling like something is wrong with her. Land on "This is information", either those words or that felt sense.

## What Happens When Love Goes Deep: 8th House
Cover: the sign on the 8th house cusp and what it reveals about her experience of true intimacy, not only sex, but the level where masks come off and real needs surface; any planets physically in the 8th house with their positions and key aspects; the 8th house ruler (sign, house, and at least one major aspect with orb), which tells you where her intimacy energy actually lives in the chart; what she most wants from deep intimacy and what she most fears in it; how sexuality and emotional connection interact for this specific chart (are they the same thing or separate for her?); and what happens to her when someone truly sees her, and what she does with that vulnerability. This section should feel intimate and honest. Write it like you are trusted to tell the truth about what lives here.

## Your Love Language From the Chart
This is not the Gary Chapman five love languages framework. This is entirely chart-derived. Cover: how Venus, Moon, and the 5th house together reveal how this person actually receives love and what makes her feel genuinely cherished; what consistently leaves her feeling unseen even when someone is trying; what she most needs to communicate to partners that is usually missed or misread; the specific contradiction in her love nature that confuses partners, named clearly so she can explain it to someone else; and one thing a partner could do tomorrow that would land more deeply than anything else based on her chart. Make this section feel like a gift: practical, warm, immediately usable. It should read like a cheat code for anyone who loves her, and like finally being understood for her.

## A Message From Your Chart
Find the single tightest Venus, Moon, or challenging love aspect in the chart. Write this section as if the chart itself is speaking directly to {chart['name']}. Second person, present tense, intimate and slightly confronting. Name the aspect and its exact orb in the first sentence. Say the thing that needed to be said, the truth the rest of the report has been building toward. It should feel like relief and reckoning at the same time. One paragraph, 5 to 8 sentences, no sub-headings. End on an invitation, not a conclusion.

## Your First Three Steps
Three concrete, specific actions. Each must be tied to a named chart placement (sign, house, or aspect with orb), be doable within the next 7 days, not be generic advice that could apply to anyone, and feel like it comes from the chart, not from a self-help book. Use ### for each step heading, in this exact order:

### Step One: [Title] (Internal / Reflective)
A journaling prompt, inner inquiry, or contemplative practice tied to a specific Moon placement or the repeating pattern. Write the actual prompt or practice, not just the concept.

### Step Two: [Title] (Relational)
Something to try, shift, or communicate in an actual relationship or in how she shows up to connection, tied to a 7th house or Venus placement with exact orb named. Be specific enough that she knows exactly what to do.

### Step Three: [Title] (Expressive)
Something creative, self-expressive, or embodied, tied to the 5th house or Mars placement. Something that reconnects her to her own desire and aliveness, not only her partnerships.

Each step should feel like an action that returns her power to her, not one that requires waiting for someone else to change.

FORMATTING RULES (follow strictly):
- Start directly with "## Your Love Signature". No title line like "# Report for [Name]".
- Use ## only for the eleven main section headings, and ### only for the three step headings in the final section.
- Do NOT use horizontal rules (---, ***, ___).
- Do NOT use **bold** as a sub-heading.
- Regular prose paragraphs only, except the three ### steps.

PUNCTUATION RULES (follow strictly):
- DO NOT use em-dashes (—) anywhere. They make prose feel AI-generated.
- DO NOT use en-dashes (–) for parentheticals.
- Use commas, full stops, semicolons, colons, or parentheses depending on what the sentence needs.

TECHNICAL ACCURACY (do not confuse these):
- A planet IS IN a house only when listed in "PLANETS IN EACH HOUSE" above. That is the only authoritative source. NEVER say a planet is "in" a house unless confirmed there.
- The house RULER governs the sign on the cusp and may or may not physically sit in that house.
- Use TRADITIONAL rulers as the primary interpretive layer (Aries/Mars, Taurus/Venus, Gemini/Mercury, Cancer/Moon, Leo/Sun, Virgo/Mercury, Libra/Venus, Scorpio/Mars, Sagittarius/Jupiter, Capricorn/Saturn, Aquarius/Saturn, Pisces/Jupiter). Modern rulers (Aquarius/Uranus, Pisces/Neptune, Scorpio/Pluto) add nuance but never replace the traditional reading.
- The 5th, 7th, and 8th house rulers must each be discussed with at least one aspect cited by exact orb (the 7th with at least two). Every Venus and Moon aspect under 3° orb must be named somewhere in the report. The tightest Venus or Moon aspect carries the most interpretive weight in the entire report."""


def build_love_email_body_html(name):
    return f"""<!DOCTYPE html>
<html><head>
<meta charset="UTF-8">
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,400;0,500;0,700;1,400;1,500;1,700&family=Mrs+Saint+Delafield&display=swap" rel="stylesheet">
</head>
<body style="margin:0;padding:0;background:#EFEBEA;font-family:'Playfair Display',Georgia,serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#EFEBEA;padding:50px 20px;">
<tr><td align="center">
  <table width="580" cellpadding="0" cellspacing="0" style="max-width:580px;">

    <tr><td style="text-align:center;padding-bottom:30px;">
      <span style="color:#AA3157;font-size:18px;letter-spacing:0.4em;">✦ ✦ ✦</span>
    </td></tr>

    <tr><td style="text-align:center;padding-bottom:36px;">
      <div style="font-family:Impact,'Arial Narrow Bold',sans-serif;font-size:42px;letter-spacing:0.02em;line-height:0.95;color:#AA3157;text-transform:uppercase;">
        THE LOVE
      </div>
      <div style="font-family:Impact,'Arial Narrow Bold',sans-serif;font-size:42px;letter-spacing:0.02em;line-height:0.95;color:#C04C2D;text-transform:uppercase;">
        BLUEPRINT
      </div>
    </td></tr>

    <tr><td style="text-align:center;padding-bottom:36px;">
      <div style="display:inline-block;width:80px;height:1px;background:#AA3157;"></div>
    </td></tr>

    <tr><td style="font-family:'Playfair Display',Georgia,serif;font-size:18px;line-height:1.85;color:#1E1E1E;text-align:left;padding:0 20px;">
      <p style="margin:0 0 22px;font-family:'Playfair Display',Georgia,serif;">Dear {name},</p>

      <p style="margin:0 0 22px;font-family:'Playfair Display',Georgia,serif;">Thank you so much for ordering your Love Blueprint. Your complete Love & Relationship report is attached as a PDF.</p>

      <p style="margin:0 0 22px;font-family:'Playfair Display',Georgia,serif;">Find a quiet moment to read it somewhere you feel at ease. My hope is that it reflects something true about how you love, and perhaps puts words to things you have always sensed but never quite named.</p>

      <p style="margin:0 0 22px;font-family:'Playfair Display',Georgia,serif;">I am so grateful for your trust. If the reading resonates, I would love to hear from you.</p>

      <p style="margin:0 0 4px;font-family:'Playfair Display',Georgia,serif;">With warmth,</p>
      <p style="margin:0 0 0;font-family:'Mrs Saint Delafield',cursive;font-size:32px;color:#AA3157;line-height:1;">Lena</p>
    </td></tr>

    <tr><td style="padding-top:50px;text-align:center;">
      <div style="display:inline-block;width:60px;height:1px;background:#AA3157;margin-bottom:18px;"></div>
      <div style="color:#AA3157;font-family:Impact,'Arial Narrow Bold',sans-serif;font-size:13px;letter-spacing:0.3em;text-transform:uppercase;">
        ✦ Lunabylena.com ✦
      </div>
      <div style="font-family:'Playfair Display',Georgia,serif;font-style:italic;font-size:12px;color:#8A7575;margin-top:8px;">
        Whole Sign houses · Swiss Ephemeris
      </div>
    </td></tr>

  </table>
</td></tr>
</table>
</body></html>"""


def build_love_pdf_html(name, report_text, birth_info, chart):
    report_body = markdown_to_html(report_text)
    name_possessive = "&#39;" if name.endswith("s") else "&#39;s"
    city_upper = birth_info["city"].upper()
    country_upper = birth_info["country"].upper()

    p = chart["planets"]
    a = chart["angles"]
    cells = [
        ("Rising", a["ASC"]["sign"], "1st"),
        ("Sun", p["Sun"]["sign"], p["Sun"]["house"]),
        ("Moon", p["Moon"]["sign"], p["Moon"]["house"]),
        ("Venus", p["Venus"]["sign"], p["Venus"]["house"]),
        ("Mars", p["Mars"]["sign"], p["Mars"]["house"]),
        ("Mercury", p["Mercury"]["sign"], p["Mercury"]["house"]),
        ("Jupiter", p["Jupiter"]["sign"], p["Jupiter"]["house"]),
        ("Saturn", p["Saturn"]["sign"], p["Saturn"]["house"]),
        ("N.Node", p["North Node"]["sign"], p["North Node"]["house"]),
        ("Chiron", p["Chiron"]["sign"], p["Chiron"]["house"]),
    ]

    def make_cell(label, value, house):
        return f'<td><div class="cell-label">{label}</div><div class="cell-value">{value}</div><div class="cell-house">{house}</div></td>'

    row1 = "".join(make_cell(*c) for c in cells[:5])
    row2 = "".join(make_cell(*c) for c in cells[5:])
    cells_html = f"<tr>{row1}</tr><tr>{row2}</tr>"

    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<style>
  @import url('https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,400;0,500;0,700;1,400;1,500;1,700&family=Mrs+Saint+Delafield&display=swap');
  @page {{ size: A4; margin: 18mm 18mm; background: #EFEBEA; }}
  * {{ box-sizing: border-box; }}
  html, body {{ margin:0;padding:0;background:#EFEBEA;font-family:'Playfair Display',Georgia,serif;color:#1E1E1E; }}
  .page {{ background:#EFEBEA; }}
  .cover {{ text-align:center;padding:50px 0 30px;page-break-after:always; }}
  .cover .stars-row {{ margin-bottom:32px;color:#AA3157;font-size:16px;letter-spacing:0.4em; }}
  .cover .brand {{ font-family:Impact,'Arial Narrow Bold',sans-serif;font-size:76px;line-height:0.92;letter-spacing:0.02em;text-transform:uppercase;margin:0; }}
  .cover .brand .line1 {{ color:#AA3157;display:block; }}
  .cover .brand .line2 {{ color:#C04C2D;display:block; }}
  .cover .tagline {{ font-family:'Playfair Display',serif;font-style:italic;font-size:14px;color:#3A3030;margin:22px 0 0;letter-spacing:0.02em; }}
  .cover-divider {{ width:80px;height:1px;background:#AA3157;margin:50px auto; }}
  .cover .eyebrow {{ display:inline-block;font-family:Impact,'Arial Narrow Bold',sans-serif;font-size:11px;letter-spacing:0.3em;text-transform:uppercase;color:#EFEBEA;background:#AA3157;padding:6px 18px;margin-bottom:28px; }}
  .cover .report-name {{ font-family:'Playfair Display',serif;font-weight:700;font-size:52px;color:#1E1E1E;margin:0 0 16px;line-height:1.05; }}
  .cover .report-name .italic {{ font-style:italic;color:#AA3157;font-weight:700; }}
  .cover .meta {{ font-family:Impact,'Arial Narrow Bold',sans-serif;font-size:10px;letter-spacing:0.22em;text-transform:uppercase;color:#7E4A92;margin:18px 0 0; }}
  .chart-strip-heading {{ font-family:Impact,'Arial Narrow Bold',sans-serif;font-size:11px;letter-spacing:0.3em;text-transform:uppercase;color:#AA3157;text-align:center;margin:0 0 18px; }}
  .chart-on-cover {{ margin-top:32px; }}
  .chart-table {{ width:100%;border-collapse:collapse;border:2px solid #1E1E1E;margin:0;table-layout:fixed; }}
  .chart-table tr {{ height:56px; }}
  .chart-table td {{ background:#EFEBEA;padding:6px 4px;text-align:center;border:1px solid #1E1E1E;width:20%;vertical-align:middle; }}
  .cell-label {{ font-family:Impact,'Arial Narrow Bold',sans-serif;font-size:8px;letter-spacing:0.18em;text-transform:uppercase;color:#7E4A92;margin-bottom:3px; }}
  .cell-value {{ font-family:'Playfair Display',serif;font-weight:500;font-size:13px;color:#1E1E1E; }}
  .cell-house {{ font-family:Impact,'Arial Narrow Bold',sans-serif;font-size:8px;color:#8A7575;margin-top:2px;letter-spacing:0.1em; }}
  .report h2 {{ font-family:Impact,'Arial Narrow Bold',sans-serif;font-size:24px;letter-spacing:0.02em;text-transform:uppercase;color:#AA3157;margin:32px 0 14px;padding-bottom:8px;border-bottom:2px solid #1E1E1E;line-height:1.05;page-break-after:avoid; }}
  .report h3 {{ font-family:'Playfair Display',serif;font-weight:700;font-style:italic;font-size:14px;color:#1E1E1E;margin:20px 0 8px;page-break-after:avoid; }}
  .report h3::before {{ content:'✦  ';color:#AA3157;font-style:normal;font-weight:400;font-size:11px; }}
  .report p {{ font-family:'Playfair Display',Georgia,serif;font-size:12px;line-height:1.75;color:#3A3030;margin:0 0 12px;text-align:left;orphans:3;widows:3; }}
  .message-callout {{ margin:36px 0 14px;padding:22px 26px;background:#FFE3EC;border:2px solid #1E1E1E;page-break-inside:avoid; }}
  .message-callout h2 {{ margin:0 0 12px;padding:0;border:none;color:#AA3157;font-size:20px; }}
  .message-callout p {{ font-style:italic;color:#1E1E1E;font-size:12.5px;line-height:1.85; }}
  .steps-section {{ margin:36px 0 14px;padding:24px 28px;background:#AA3157;color:#EFEBEA;border:2px solid #1E1E1E; }}
  .steps-section h2 {{ margin-top:0;color:#EFEBEA;border-bottom:2px solid #EFEBEA; }}
  .steps-section h3 {{ color:#EFEBEA;font-style:normal;font-family:Impact,'Arial Narrow Bold',sans-serif;font-size:11px;letter-spacing:0.2em;text-transform:uppercase;margin-top:18px; }}
  .steps-section h3::before {{ color:#EFEBEA; }}
  .steps-section p {{ color:#EFEBEA;font-style:italic; }}
  .footer {{ margin-top:50px;padding-top:20px;border-top:1px solid #AA3157;text-align:center; }}
  .footer-label {{ font-family:Impact,'Arial Narrow Bold',sans-serif;font-size:10px;letter-spacing:0.3em;text-transform:uppercase;color:#AA3157; }}
  .footer-note {{ font-family:'Playfair Display',serif;font-style:italic;font-size:9px;color:#8A7575;margin-top:4px; }}
</style>
</head>
<body>
<div class="page">
  <div class="cover">
    <div class="stars-row">✦ ✦ ✦</div>
    <h1 class="brand">
      <span class="line1">THE LOVE</span>
      <span class="line2">BLUEPRINT</span>
    </h1>
    <p class="tagline">Love · Desire · Partnership · Intimacy</p>
    <div class="cover-divider"></div>
    <span class="eyebrow">The Love Blueprint</span>
    <h2 class="report-name">{name}{name_possessive} <span class="italic">Love Blueprint</span></h2>
    <p class="meta">{birth_info['date']} · {birth_info['time']} · {city_upper}, {country_upper}</p>
    <div class="chart-on-cover">
      <p class="chart-strip-heading">Your Chart at a Glance</p>
      <table class="chart-table">{cells_html}</table>
    </div>
  </div>
  <div class="report">{report_body}</div>
  <div class="footer">
    <div class="footer-label">✦ Lunabylena.com ✦</div>
    <div class="footer-note">Whole Sign houses · Swiss Ephemeris</div>
  </div>
</div>
</body></html>"""


def background_generate_and_send_love(email, chart, birth_info):
    try:
        prompt = build_love_prompt(chart, birth_info, preview_only=False)
        report_text = generate_full_report(prompt)
        pdf_html = build_love_pdf_html(chart["name"], report_text, birth_info, chart)
        pdf_bytes = generate_pdf(pdf_html)
        email_body = build_love_email_body_html(chart["name"])
        send_report_email(
            email, chart["name"], email_body, pdf_bytes,
            subject=f"Your Love Blueprint ✦ {chart['name']}",
            filename=f"{chart['name']}-love-blueprint.pdf"
        )
    except Exception as e:
        print(f"Love background generation failed: {e}")


# ─────────────────────────────────────────────
#  LOVE BLUEPRINT — routes
# ─────────────────────────────────────────────

@app.route("/identity-recode")
def identity_recode():
    return render_template("identity_recode_v2.html")


@app.route("/identity-recode-v2")
def identity_recode_v2():
    return redirect("/identity-recode")


@app.route("/identity-recode-new")
def identity_recode_new():
    return render_template("identity_recode_new.html")


@app.route("/love")
def love():
    return render_template("love.html",
        auto_generate=False,
        chart_data="null",
        meta_data="null")


@app.route("/love/generate", methods=["POST"])
def love_generate():
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

    log_customer(name=name, email=email, marketing_opt_in=marketing_opt_in,
                 date=date_str, city=city, country=country, tag_name="love-blueprint")

    try:
        year, month, day = [int(x) for x in date_str.split("-")]
        hour, minute = [int(x) for x in time_str.split(":")]
        lat, lng = float(lat), float(lng)
    except Exception:
        return jsonify({"error": "Invalid birth details."}), 400

    try:
        chart = calculate_chart(name, year, month, day, hour, minute, lat, lng, tz_str)
    except Exception as e:
        return jsonify({"error": f"Chart calculation failed: {str(e)}"}), 500

    birth_info = {"date": date_str, "time": time_str, "city": city, "country": country}
    preview_prompt = build_love_prompt(chart, birth_info, preview_only=True)
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY",""))

    def stream():
        yield f"data: {json.dumps({'type':'chart','data':chart})}\n\n"
        buffer = ""
        with client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            messages=[{"role":"user","content":preview_prompt}]
        ) as st:
            for text in st.text_stream:
                buffer += text
                if len(buffer) > 3:
                    flush = buffer[:-3]; buffer = buffer[-3:]
                    yield f"data: {json.dumps({'type':'text','content':clean_dashes(flush)})}\n\n"
        if buffer:
            yield f"data: {json.dumps({'type':'text','content':clean_dashes(buffer)})}\n\n"
        yield f"data: {json.dumps({'type':'done','email':email})}\n\n"

    return Response(stream(), mimetype="text/event-stream",
                   headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})


@app.route("/love/create-checkout-session", methods=["POST"])
def love_create_checkout_session():
    import stripe as stripe_lib
    stripe_lib.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
    data = request.json

    name = data.get("name", "").strip()
    email = data.get("email", "").strip()
    if not name or not email or "@" not in email:
        return jsonify({"error": "Please provide your name and a valid email address."}), 400
    if not data.get("date") or not data.get("time"):
        return jsonify({"error": "Please provide your birth date and time."}), 400
    if not data.get("lat") or not data.get("lng"):
        return jsonify({"error": "Please select a city from the dropdown."}), 400

    try:
        session = stripe_lib.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "eur",
                    "unit_amount": PRICE_EUR,
                    "product_data": {
                        "name": "The Love Blueprint",
                        "description": "Your personalised astrology report — Love, Desire & Relationship patterns from your birth chart",
                    },
                },
                "quantity": 1,
            }],
            mode="payment",
            customer_email=email,
            success_url=f"{request.host_url}love/payment-success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{request.host_url}love?cancelled=true",
            metadata={
                "name": name,
                "email": email,
                "date": data.get("date", ""),
                "time": data.get("time", ""),
                "city": data.get("city", ""),
                "country": data.get("country", ""),
                "lat": str(data.get("lat", "")),
                "lng": str(data.get("lng", "")),
                "tz": data.get("tz", "UTC"),
                "marketingOptIn": "true" if data.get("marketingOptIn") else "false",
                "report": "love-blueprint",
            }
        )
        return jsonify({"url": session.url})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/love/payment-success")
def love_payment_success():
    session_id = request.args.get("session_id")
    if not session_id:
        return render_template("love.html",
            auto_generate=False, chart_data="null", meta_data="null")
    try:
        import stripe as stripe_lib
        stripe_lib.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
        session = stripe_lib.checkout.Session.retrieve(session_id)
        if session.payment_status != "paid":
            return render_template("love.html",
                auto_generate=False, chart_data="null", meta_data="null")
        meta = session.metadata.to_dict()
        return render_template("love_thank_you.html",
            session_id=session_id,
            name=meta.get("name", ""),
            email=meta.get("email", ""))
    except Exception as e:
        print(f"Love payment success error: {e}")
        return render_template("love.html",
            auto_generate=False, chart_data="null", meta_data="null")


@app.route("/love/generate-after-payment", methods=["POST"])
def love_generate_after_payment():
    import stripe as stripe_lib
    stripe_lib.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
    data = request.json
    session_id = data.get("session_id", "")

    try:
        session = stripe_lib.checkout.Session.retrieve(session_id)
    except Exception as e:
        return jsonify({"error": f"Could not verify payment: {type(e).__name__}: {e}"}), 400

    if session.payment_status != "paid":
        return jsonify({"error": "Payment not complete."}), 400

    meta = session.metadata.to_dict()
    name = meta.get("name", "") or "the person"
    email = meta.get("email", "")
    date_str = meta.get("date", "")
    time_str = meta.get("time", "")
    city = meta.get("city", "")
    country = meta.get("country", "")
    tz_str = meta.get("tz", "UTC")
    marketing_opt_in = meta.get("marketingOptIn") == "true"

    try:
        lat = float(meta.get("lat", "0"))
        lng = float(meta.get("lng", "0"))
        year, month, day = [int(x) for x in date_str.split("-")]
        hour, minute = [int(x) for x in time_str.split(":")]
    except Exception as e:
        return jsonify({"error": f"Invalid birth data: {e}"}), 400

    try:
        chart = calculate_chart(name, year, month, day, hour, minute, lat, lng, tz_str)
    except Exception as e:
        return jsonify({"error": f"Chart calculation failed: {e}"}), 500

    birth_info = {"date": date_str, "time": time_str, "city": city, "country": country}
    log_customer(name=name, email=email, marketing_opt_in=marketing_opt_in,
                date=date_str, city=city, country=country, tag_name="love-blueprint")

    thread = threading.Thread(
        target=background_generate_and_send_love,
        args=(email, chart, birth_info),
        daemon=True
    )
    thread.start()

    preview_prompt = build_love_prompt(chart, birth_info, preview_only=True)
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

    def stream():
        chart_event = dict(chart)
        yield f"data: {json.dumps({'type':'chart','data':chart_event,'payload':{'name':name,'email':email,'date':date_str,'time':time_str,'city':city,'country':country,'lat':lat,'lng':lng,'tz':tz_str}})}\n\n"
        buffer = ""
        with client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            messages=[{"role":"user","content":preview_prompt}]
        ) as st:
            for text in st.text_stream:
                buffer += text
                if len(buffer) > 3:
                    flush = buffer[:-3]; buffer = buffer[-3:]
                    yield f"data: {json.dumps({'type':'text','content':clean_dashes(flush)})}\n\n"
        if buffer:
            yield f"data: {json.dumps({'type':'text','content':clean_dashes(buffer)})}\n\n"
        yield f"data: {json.dumps({'type':'done','email':email})}\n\n"

    return Response(stream(), mimetype="text/event-stream",
                   headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})


# ═════════════════════════════════════════════
#  SHADOW BLUEPRINT
# ═════════════════════════════════════════════

def build_shadow_prompt(chart, birth_info, preview_only=False):
    pd = chart["planets"]
    a = chart["angles"]
    hr = chart["house_rulers"]
    aspects = chart["aspects"]
    language_guidance = build_language_guidance(
        chart.get("dominant_element", "earth"),
        chart.get("asc_element", "earth"),
        chart.get("element_balance", {"fire":25,"earth":25,"air":25,"water":25})
    )

    planet_lines = [f"  - {n}: {d['sign']}, {d['house']} house, {d['position']}°" for n,d in pd.items()]

    house_occupants = {h: [] for h in range(1, 13)}
    house_num_map = {"1st":1,"2nd":2,"3rd":3,"4th":4,"5th":5,"6th":6,"7th":7,"8th":8,"9th":9,"10th":10,"11th":11,"12th":12}
    for pname, pdata in pd.items():
        h_num = house_num_map.get(pdata["house"])
        if h_num:
            house_occupants[h_num].append(f"{pname} ({pdata['sign']} {pdata['position']}°)")

    ws_signs = ["Aries","Taurus","Gemini","Cancer","Leo","Virgo","Libra","Scorpio","Sagittarius","Capricorn","Aquarius","Pisces"]
    asc_idx_h = ws_signs.index(a["ASC"]["sign"])

    def ordinal(n):
        return {1:"1st",2:"2nd",3:"3rd",4:"4th",5:"5th",6:"6th",7:"7th",8:"8th",9:"9th",10:"10th",11:"11th",12:"12th"}.get(n, f"{n}th")

    occupants_lines = []
    for h in range(1, 13):
        sign_on_cusp = ws_signs[(asc_idx_h + h - 1) % 12]
        occupants = house_occupants[h]
        h_ord = ordinal(h)
        tag = f": {', '.join(occupants)}" if occupants else ": EMPTY (no planets)"
        occupants_lines.append(f"  - {h_ord} house ({sign_on_cusp} on cusp){tag}")

    def describe_ruler(ruler_name, h_ord_label=None):
        rd = pd.get(ruler_name, {})
        rs, rh, rp = rd.get("sign","?"), rd.get("house","?"), rd.get("position","?")
        if h_ord_label and rh == h_ord_label:
            return f"{ruler_name} sits in {rs} at {rp}° in the {rh} house (ruler IS in its own house here)"
        elif h_ord_label:
            return f"{ruler_name} sits in {rs} at {rp}° in the {rh} house (ruler is NOT in the {h_ord_label} house, it is in the {rh})"
        return f"{ruler_name} sits in {rs} at {rp}° in the {rh} house"

    shadow_ruler_lines = []
    for h in [4, 8, 12]:
        if h not in hr:
            continue
        h_ord = ordinal(h)
        trad = hr[h]["ruler"]
        mod = hr[h].get("modern_ruler")
        line = f"  - {h_ord} house ({hr[h]['sign']} on cusp): TRADITIONAL ruler is {trad}. {describe_ruler(trad, h_ord)}"
        if mod:
            line += f"\n      Modern co-ruler is {mod}. {describe_ruler(mod, h_ord)}"
        shadow_ruler_lines.append(line)

    # Key points for shadow work
    shadow_key = ["Pluto", "Saturn", "Chiron", "Moon", "South Node", "North Node", "Neptune"]
    for h in [4, 8, 12]:
        if h in hr:
            for k in [hr[h]["ruler"], hr[h].get("modern_ruler")]:
                if k and k not in shadow_key:
                    shadow_key.append(k)

    shadow_asp_by_planet = {}
    for asp in aspects:
        for side in [asp["p1"], asp["p2"]]:
            if side in shadow_key:
                other = asp["p2"] if side == asp["p1"] else asp["p1"]
                shadow_asp_by_planet.setdefault(side, []).append(f"{asp['aspect']} {other} ({asp['orb']}°)")

    shadow_summary_lines = []
    for kp in shadow_key:
        if kp in shadow_asp_by_planet:
            shadow_summary_lines.append(f"  - {kp}: {'; '.join(shadow_asp_by_planet[kp][:5])}")

    aspect_lines = [f"  - {x['p1']} {x['aspect']} {x['p2']} (orb: {x['orb']}°)" for x in aspects[:25]]

    chart_data = f"""BIRTH DETAILS: {chart['name']}, {birth_info['date']}, {birth_info['time']}, {birth_info['city']}, {birth_info['country']}
House System: Whole Sign

PLANETS BY POSITION:
{chr(10).join(planet_lines)}

PLANETS IN EACH HOUSE (AUTHORITATIVE — use ONLY this for "planets in X house" statements):
{chr(10).join(occupants_lines)}

ANGLES:
  - ASC: {a['ASC']['sign']} {a['ASC']['position']}°
  - MC: {a['MC']['sign']} {a['MC']['position']}° (Whole Sign house {a['MC']['ws_house']})
  - IC: {a['IC']['sign']} {a['IC']['position']}° (Whole Sign house {a['IC']['ws_house']})

SHADOW-RELEVANT HOUSE RULERS — 4th (roots/the origin), 8th (depth/transformation), 12th (the unconscious/the hidden):
(Use TRADITIONAL rulers as primary; modern co-rulers add nuance but never replace the traditional reading)
{chr(10).join(shadow_ruler_lines)}

KEY ASPECTS BY PLANET (shadow-relevant points — Pluto, Saturn, Chiron, Moon, the Nodes — use these for aspect citations):
{chr(10).join(shadow_summary_lines)}

ALL KEY ASPECTS (tightest first — the tightest challenging aspect is the throughline):
{chr(10).join(aspect_lines)}"""

    if preview_only:
        return f"""You are writing the opening section, "Your Shadow Signature", of a premium shadow-work natal chart report called The Shadow Blueprint, for {chart['name']}. Second person.

VOICE AND TONE (non-negotiable):
Write as a wise, warm, direct therapist who also happens to read charts, not a doom-and-gloom astrologer. Never pathologise: the shadow is not damage, it is information. Never use the words: toxic, broken, damaged, blocked, negative. Use challenging, unconscious, unintegrated, protective instead. Every sentence carries the implicit message: this is information, not a verdict.

CRITICAL, ADAPT LANGUAGE TO THIS CHART (sign and dominant element / triplicity):
{language_guidance}

{chart_data}

Write ONLY the "Your Shadow Signature" section. EXACTLY 2 to 3 sentences. No heading, no preamble.
Capture the essence of the shadow landscape, drawn from Pluto's sign and house, Saturn's sign and house, Chiron's sign and house, and the Moon's most challenging aspect (named with its exact orb). Plant the throughline that the wound and the gift are the same placement seen from a different angle. Make it specific enough to this exact chart that it could not describe anyone else. Tie every claim to a named placement, and name any aspect with its exact orb. Do not use em-dashes. Do not name the elemental register explicitly. Output only the paragraph text."""

    return f"""You are writing a premium, deeply personal shadow-work report called The Shadow Blueprint for {chart['name']}. This report reads the natal chart to reveal the unconscious patterns, the original wound, and the buried power a person carries, and hands each one back as information she can use. Second person throughout.

VOICE AND TONE (non-negotiable):
Write as a wise, warm, direct therapist who also happens to read charts, not a doom-and-gloom astrologer.
- Never pathologise. Shadow is not damage. Every sentence should carry the implicit message: this is information, not a verdict.
- Never use the words: toxic, broken, damaged, blocked, negative. Use challenging, unconscious, unintegrated, protective instead.
- Signature phrases available, use sparingly and never force them: "This is information." "Nothing has gone wrong." "Without shame."

CRITICAL, ADAPT LANGUAGE TO THIS CHART (sign and dominant element / triplicity, earth / water / fire / air):
{language_guidance}

{chart_data}

THE THROUGHLINE (weave throughout, do not confine to one section):
The wound and the gift are the same placement seen from a different angle. Return to this idea across the whole report the way the tightest aspect functions as the throughline. The single tightest challenging aspect in the chart carries the most interpretive weight: name it early and return to it.

Write the report in the following sections, in this exact order, using ## for each section heading:

## Your Shadow Signature
2 to 3 sentences. The essence of the shadow landscape, drawn from Pluto's sign and house, Saturn's sign and house, Chiron's sign and house, and the Moon's most challenging aspect (named with exact orb). This sets the tone for the entire report. Plant the wound-and-gift throughline here.

## The Wound: Chiron
Chiron's sign and house. Where the original wound formed and how it shows up as a recurring sensitivity in adult life, specific to this Chiron placement. State explicitly, here and not deferred, that the wound and the gift are the same placement. Name Chiron's tightest aspect with its exact orb.

## The Pattern: South Node & Saturn
Read the South Node and Saturn together, not separately. South Node is the comfortable over-reliance, the old default. Saturn is the fear and the "not enough" voice. Together they describe the loop. Frame it as "here is the loop, and here is why it made sense", never "you are stuck". Name each placement by sign and house, and cite at least one Saturn aspect with its exact orb.

## The Hidden Room: The 12th House
The 12th house is the unconscious, the hidden, what operates below awareness. Cover the sign on the 12th cusp, any planets physically in the 12th (with positions and aspects), and the 12th house ruler. The ruler discussion is MANDATORY and must live inside this section itself: name the ruler, the sign and house it sits in, and at least one aspect with exact orb. If the 12th is empty of planets, read the house entirely through the ruler's placement and aspects. This should feel like a door opening, not a verdict.

## The Transformer: Pluto
Pluto's sign, house, and aspects. The compulsive quality, what she cannot stop returning to even when it costs her, specific to this Pluto placement. The tightest Pluto aspect MUST be named explicitly with its exact orb and interpreted in depth.

## How Your Shadow Speaks
The most practical section. Using only the placements already named, name specific triggers and relationship behaviours unique to this chart, in the pattern "When you feel [X], you tend to [Y], because [named placement]." Zero generic shadow-work language: every claim ties to a named placement.

## The Hidden Gift
Invert every shadow placement: Chiron's wound as the healer's gift, Saturn's restriction as the builder's discipline, Pluto's compulsion as the transformer's power, the 12th house exile as the mystic's depth. This reframes the whole report. Genuinely uplifting, grounded in the exact placements, never saccharine.

## A Message From Your Shadow
Written from the single tightest challenging aspect in the entire chart (tightest square, opposition, or Pluto / Saturn / Chiron contact). Name the aspect and its exact orb in the first sentence. Second person, present tense, the chart speaking directly to her: direct, personal, intimate. One paragraph, 5 to 8 sentences, no sub-headings. End on an invitation, not a conclusion.

## Your Integration Practices
Three practices, in this exact order, each naming the exact placement it addresses and each specific enough that it could not apply to a different chart. Use ### for each practice heading.

### The Internal Practice
An internal or reflective practice (a journaling prompt, meditation, or inner-child inquiry) tied to a specific Moon placement or the repeating pattern. Write the actual prompt or practice, not just the concept.

### The Somatic Practice
A somatic or embodied practice (movement, breath, something in the body) tied to a named placement, for example Pluto, Mars, or Saturn.

### The Relational Practice
A relational practice, how to bring this awareness into a real relationship, tied to a named placement, for example "Because your Chiron is in the 7th house in Libra, the relational practice that will move the needle most is...".

FORMATTING RULES (follow strictly):
- Start directly with "## Your Shadow Signature". No title line like "# Report for [Name]".
- Use ## only for the nine main section headings, and ### only for the three practice headings in the final section.
- Do NOT use horizontal rules (---, ***, ___).
- Do NOT use **bold** as a sub-heading.
- Regular prose paragraphs only, except the three ### practices.

PUNCTUATION RULES (follow strictly):
- DO NOT use em-dashes (—) anywhere. They make prose feel AI-generated.
- DO NOT use en-dashes (–) for parentheticals.
- Use commas, full stops, semicolons, colons, or parentheses depending on what the sentence needs.

TECHNICAL ACCURACY (do not confuse these):
- A planet IS IN a house only when listed in "PLANETS IN EACH HOUSE" above. That is the only authoritative source. NEVER say a planet is "in" a house unless confirmed there.
- The house RULER governs the sign on the cusp and may or may not physically sit in that house. The 12th house section must discuss BOTH occupancy AND the ruler, inside that section, never delegating the ruler to wherever it happens to sit elsewhere in the report.
- Use TRADITIONAL rulers as the primary interpretive layer (Aries/Mars, Taurus/Venus, Gemini/Mercury, Cancer/Moon, Leo/Sun, Virgo/Mercury, Libra/Venus, Scorpio/Mars, Sagittarius/Jupiter, Capricorn/Saturn, Aquarius/Saturn, Pisces/Jupiter). Modern rulers (Aquarius/Uranus, Pisces/Neptune, Scorpio/Pluto) add nuance but never replace the traditional reading.

SPECIFICITY (non-negotiable):
- Every interpretive claim carries sign, house, and exact orb where an aspect is involved. "Pluto square Moon" is not acceptable on its own: it must read "Pluto in [sign] in the [house] house square Moon at [X]°".
- Every major aspect is named with its exact orb every time it appears.
- Beyond the wound-and-gift throughline, each section introduces new information. If a placement is fully explored in one section, do not re-explain it from scratch in another: only reference it when it adds genuinely new meaning in a different context."""


def build_shadow_email_body_html(name):
    return f"""<!DOCTYPE html>
<html><head>
<meta charset="UTF-8">
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,400;0,500;0,700;1,400;1,500;1,700&family=Mrs+Saint+Delafield&display=swap" rel="stylesheet">
</head>
<body style="margin:0;padding:0;background:#EFEBEA;font-family:'Playfair Display',Georgia,serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#EFEBEA;padding:50px 20px;">
<tr><td align="center">
  <table width="580" cellpadding="0" cellspacing="0" style="max-width:580px;">

    <tr><td style="text-align:center;padding-bottom:30px;">
      <span style="color:#AA3157;font-size:18px;letter-spacing:0.4em;">✦ ✦ ✦</span>
    </td></tr>

    <tr><td style="text-align:center;padding-bottom:36px;">
      <div style="font-family:Impact,'Arial Narrow Bold',sans-serif;font-size:42px;letter-spacing:0.02em;line-height:0.95;color:#AA3157;text-transform:uppercase;">
        THE SHADOW
      </div>
      <div style="font-family:Impact,'Arial Narrow Bold',sans-serif;font-size:42px;letter-spacing:0.02em;line-height:0.95;color:#C04C2D;text-transform:uppercase;">
        BLUEPRINT
      </div>
    </td></tr>

    <tr><td style="text-align:center;padding-bottom:36px;">
      <div style="display:inline-block;width:80px;height:1px;background:#AA3157;"></div>
    </td></tr>

    <tr><td style="font-family:'Playfair Display',Georgia,serif;font-size:18px;line-height:1.85;color:#1E1E1E;text-align:left;padding:0 20px;">
      <p style="margin:0 0 22px;font-family:'Playfair Display',Georgia,serif;">Dear {name},</p>

      <p style="margin:0 0 22px;font-family:'Playfair Display',Georgia,serif;">Thank you so much for ordering your Shadow Blueprint. Your complete report is attached as a PDF.</p>

      <p style="margin:0 0 22px;font-family:'Playfair Display',Georgia,serif;">Find a quiet, unhurried moment to read it. This report meets the parts of you that usually stay in the dark, and it does so without shame. Nothing here is a verdict. It is all information you can use.</p>

      <p style="margin:0 0 22px;font-family:'Playfair Display',Georgia,serif;">I am so grateful for your trust. If the reading resonates, I would love to hear from you.</p>

      <p style="margin:0 0 4px;font-family:'Playfair Display',Georgia,serif;">With warmth,</p>
      <p style="margin:0 0 0;font-family:'Mrs Saint Delafield',cursive;font-size:32px;color:#AA3157;line-height:1;">Lena</p>
    </td></tr>

    <tr><td style="padding-top:50px;text-align:center;">
      <div style="display:inline-block;width:60px;height:1px;background:#AA3157;margin-bottom:18px;"></div>
      <div style="color:#AA3157;font-family:Impact,'Arial Narrow Bold',sans-serif;font-size:13px;letter-spacing:0.3em;text-transform:uppercase;">
        ✦ Lunabylena.com ✦
      </div>
      <div style="font-family:'Playfair Display',Georgia,serif;font-style:italic;font-size:12px;color:#8A7575;margin-top:8px;">
        Whole Sign houses · Swiss Ephemeris
      </div>
    </td></tr>

  </table>
</td></tr>
</table>
</body></html>"""


def build_shadow_pdf_html(name, report_text, birth_info, chart):
    report_body = markdown_to_html(report_text)
    name_possessive = "&#39;" if name.endswith("s") else "&#39;s"
    city_upper = birth_info["city"].upper()
    country_upper = birth_info["country"].upper()

    p = chart["planets"]
    a = chart["angles"]
    cells = [
        ("Rising", a["ASC"]["sign"], "1st"),
        ("Sun", p["Sun"]["sign"], p["Sun"]["house"]),
        ("Moon", p["Moon"]["sign"], p["Moon"]["house"]),
        ("Mars", p["Mars"]["sign"], p["Mars"]["house"]),
        ("Saturn", p["Saturn"]["sign"], p["Saturn"]["house"]),
        ("Neptune", p["Neptune"]["sign"], p["Neptune"]["house"]),
        ("Pluto", p["Pluto"]["sign"], p["Pluto"]["house"]),
        ("S.Node", p["South Node"]["sign"], p["South Node"]["house"]),
        ("N.Node", p["North Node"]["sign"], p["North Node"]["house"]),
        ("Chiron", p["Chiron"]["sign"], p["Chiron"]["house"]),
    ]

    def make_cell(label, value, house):
        return f'<td><div class="cell-label">{label}</div><div class="cell-value">{value}</div><div class="cell-house">{house}</div></td>'

    row1 = "".join(make_cell(*c) for c in cells[:5])
    row2 = "".join(make_cell(*c) for c in cells[5:])
    cells_html = f"<tr>{row1}</tr><tr>{row2}</tr>"

    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<style>
  @import url('https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,400;0,500;0,700;1,400;1,500;1,700&family=Mrs+Saint+Delafield&display=swap');
  @page {{ size: A4; margin: 18mm 18mm; background: #EFEBEA; }}
  * {{ box-sizing: border-box; }}
  html, body {{ margin:0;padding:0;background:#EFEBEA;font-family:'Playfair Display',Georgia,serif;color:#1E1E1E; }}
  .page {{ background:#EFEBEA; }}
  .cover {{ text-align:center;padding:50px 0 30px;page-break-after:always; }}
  .cover .stars-row {{ margin-bottom:32px;color:#AA3157;font-size:16px;letter-spacing:0.4em; }}
  .cover .brand {{ font-family:Impact,'Arial Narrow Bold',sans-serif;font-size:76px;line-height:0.92;letter-spacing:0.02em;text-transform:uppercase;margin:0; }}
  .cover .brand .line1 {{ color:#AA3157;display:block; }}
  .cover .brand .line2 {{ color:#C04C2D;display:block; }}
  .cover .tagline {{ font-family:'Playfair Display',serif;font-style:italic;font-size:14px;color:#3A3030;margin:22px 0 0;letter-spacing:0.02em; }}
  .cover-divider {{ width:80px;height:1px;background:#AA3157;margin:50px auto; }}
  .cover .eyebrow {{ display:inline-block;font-family:Impact,'Arial Narrow Bold',sans-serif;font-size:11px;letter-spacing:0.3em;text-transform:uppercase;color:#EFEBEA;background:#AA3157;padding:6px 18px;margin-bottom:28px; }}
  .cover .report-name {{ font-family:'Playfair Display',serif;font-weight:700;font-size:52px;color:#1E1E1E;margin:0 0 16px;line-height:1.05; }}
  .cover .report-name .italic {{ font-style:italic;color:#AA3157;font-weight:700; }}
  .cover .meta {{ font-family:Impact,'Arial Narrow Bold',sans-serif;font-size:10px;letter-spacing:0.22em;text-transform:uppercase;color:#7E4A92;margin:18px 0 0; }}
  .chart-strip-heading {{ font-family:Impact,'Arial Narrow Bold',sans-serif;font-size:11px;letter-spacing:0.3em;text-transform:uppercase;color:#AA3157;text-align:center;margin:0 0 18px; }}
  .chart-on-cover {{ margin-top:32px; }}
  .chart-table {{ width:100%;border-collapse:collapse;border:2px solid #1E1E1E;margin:0;table-layout:fixed; }}
  .chart-table tr {{ height:56px; }}
  .chart-table td {{ background:#EFEBEA;padding:6px 4px;text-align:center;border:1px solid #1E1E1E;width:20%;vertical-align:middle; }}
  .cell-label {{ font-family:Impact,'Arial Narrow Bold',sans-serif;font-size:8px;letter-spacing:0.18em;text-transform:uppercase;color:#7E4A92;margin-bottom:3px; }}
  .cell-value {{ font-family:'Playfair Display',serif;font-weight:500;font-size:13px;color:#1E1E1E; }}
  .cell-house {{ font-family:Impact,'Arial Narrow Bold',sans-serif;font-size:8px;color:#8A7575;margin-top:2px;letter-spacing:0.1em; }}
  .report h2 {{ font-family:Impact,'Arial Narrow Bold',sans-serif;font-size:24px;letter-spacing:0.02em;text-transform:uppercase;color:#AA3157;margin:32px 0 14px;padding-bottom:8px;border-bottom:2px solid #1E1E1E;line-height:1.05;page-break-after:avoid; }}
  .report h3 {{ font-family:'Playfair Display',serif;font-weight:700;font-style:italic;font-size:14px;color:#1E1E1E;margin:20px 0 8px;page-break-after:avoid; }}
  .report h3::before {{ content:'✦  ';color:#AA3157;font-style:normal;font-weight:400;font-size:11px; }}
  .report p {{ font-family:'Playfair Display',Georgia,serif;font-size:12px;line-height:1.75;color:#3A3030;margin:0 0 12px;text-align:left;orphans:3;widows:3; }}
  .message-callout {{ margin:36px 0 14px;padding:22px 26px;background:#FFE3EC;border:2px solid #1E1E1E;page-break-inside:avoid; }}
  .message-callout h2 {{ margin:0 0 12px;padding:0;border:none;color:#AA3157;font-size:20px; }}
  .message-callout p {{ font-style:italic;color:#1E1E1E;font-size:12.5px;line-height:1.85; }}
  .steps-section {{ margin:36px 0 14px;padding:24px 28px;background:#AA3157;color:#EFEBEA;border:2px solid #1E1E1E; }}
  .steps-section h2 {{ margin-top:0;color:#EFEBEA;border-bottom:2px solid #EFEBEA; }}
  .steps-section h3 {{ color:#EFEBEA;font-style:normal;font-family:Impact,'Arial Narrow Bold',sans-serif;font-size:11px;letter-spacing:0.2em;text-transform:uppercase;margin-top:18px; }}
  .steps-section h3::before {{ color:#EFEBEA; }}
  .steps-section p {{ color:#EFEBEA;font-style:italic; }}
  .footer {{ margin-top:50px;padding-top:20px;border-top:1px solid #AA3157;text-align:center; }}
  .footer-label {{ font-family:Impact,'Arial Narrow Bold',sans-serif;font-size:10px;letter-spacing:0.3em;text-transform:uppercase;color:#AA3157; }}
  .footer-note {{ font-family:'Playfair Display',serif;font-style:italic;font-size:9px;color:#8A7575;margin-top:4px; }}
</style>
</head>
<body>
<div class="page">
  <div class="cover">
    <div class="stars-row">✦ ✦ ✦</div>
    <h1 class="brand">
      <span class="line1">THE SHADOW</span>
      <span class="line2">BLUEPRINT</span>
    </h1>
    <p class="tagline">The Wound · The Pattern · The Hidden Gift</p>
    <div class="cover-divider"></div>
    <span class="eyebrow">The Shadow Blueprint</span>
    <h2 class="report-name">{name}{name_possessive} <span class="italic">Shadow Blueprint</span></h2>
    <p class="meta">{birth_info['date']} · {birth_info['time']} · {city_upper}, {country_upper}</p>
    <div class="chart-on-cover">
      <p class="chart-strip-heading">Your Chart at a Glance</p>
      <table class="chart-table">{cells_html}</table>
    </div>
  </div>
  <div class="report">{report_body}</div>
  <div class="footer">
    <div class="footer-label">✦ Lunabylena.com ✦</div>
    <div class="footer-note">Whole Sign houses · Swiss Ephemeris</div>
  </div>
</div>
</body></html>"""


def background_generate_and_send_shadow(email, chart, birth_info):
    try:
        prompt = build_shadow_prompt(chart, birth_info, preview_only=False)
        report_text = generate_full_report(prompt)
        pdf_html = build_shadow_pdf_html(chart["name"], report_text, birth_info, chart)
        pdf_bytes = generate_pdf(pdf_html)
        email_body = build_shadow_email_body_html(chart["name"])
        send_report_email(
            email, chart["name"], email_body, pdf_bytes,
            subject=f"Your Shadow Blueprint ✦ {chart['name']}",
            filename=f"{chart['name']}-shadow-blueprint.pdf"
        )
    except Exception as e:
        print(f"Shadow background generation failed: {e}")


# ─────────────────────────────────────────────
#  SHADOW BLUEPRINT — routes
# ─────────────────────────────────────────────

@app.route("/shadow")
def shadow():
    return render_template("shadow.html",
        auto_generate=False,
        chart_data="null",
        meta_data="null")


@app.route("/shadow/generate", methods=["POST"])
def shadow_generate():
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

    log_customer(name=name, email=email, marketing_opt_in=marketing_opt_in,
                 date=date_str, city=city, country=country, tag_name="shadow-blueprint")

    try:
        year, month, day = [int(x) for x in date_str.split("-")]
        hour, minute = [int(x) for x in time_str.split(":")]
        lat, lng = float(lat), float(lng)
    except Exception:
        return jsonify({"error": "Invalid birth details."}), 400

    try:
        chart = calculate_chart(name, year, month, day, hour, minute, lat, lng, tz_str)
    except Exception as e:
        return jsonify({"error": f"Chart calculation failed: {str(e)}"}), 500

    birth_info = {"date": date_str, "time": time_str, "city": city, "country": country}
    preview_prompt = build_shadow_prompt(chart, birth_info, preview_only=True)
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY",""))

    def stream():
        yield f"data: {json.dumps({'type':'chart','data':chart})}\n\n"
        buffer = ""
        with client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            messages=[{"role":"user","content":preview_prompt}]
        ) as st:
            for text in st.text_stream:
                buffer += text
                if len(buffer) > 3:
                    flush = buffer[:-3]; buffer = buffer[-3:]
                    yield f"data: {json.dumps({'type':'text','content':clean_dashes(flush)})}\n\n"
        if buffer:
            yield f"data: {json.dumps({'type':'text','content':clean_dashes(buffer)})}\n\n"
        yield f"data: {json.dumps({'type':'done','email':email})}\n\n"

    return Response(stream(), mimetype="text/event-stream",
                   headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})


@app.route("/shadow/create-checkout-session", methods=["POST"])
def shadow_create_checkout_session():
    import stripe as stripe_lib
    stripe_lib.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
    data = request.json

    name = data.get("name", "").strip()
    email = data.get("email", "").strip()
    if not name or not email or "@" not in email:
        return jsonify({"error": "Please provide your name and a valid email address."}), 400
    if not data.get("date") or not data.get("time"):
        return jsonify({"error": "Please provide your birth date and time."}), 400
    if not data.get("lat") or not data.get("lng"):
        return jsonify({"error": "Please select a city from the dropdown."}), 400

    try:
        session = stripe_lib.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "eur",
                    "unit_amount": PRICE_EUR,
                    "product_data": {
                        "name": "The Shadow Blueprint",
                        "description": "Your personalised shadow-work astrology report, the wound, the pattern, and the hidden gift in your birth chart",
                    },
                },
                "quantity": 1,
            }],
            mode="payment",
            customer_email=email,
            success_url=f"{request.host_url}shadow/payment-success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{request.host_url}shadow?cancelled=true",
            metadata={
                "name": name,
                "email": email,
                "date": data.get("date", ""),
                "time": data.get("time", ""),
                "city": data.get("city", ""),
                "country": data.get("country", ""),
                "lat": str(data.get("lat", "")),
                "lng": str(data.get("lng", "")),
                "tz": data.get("tz", "UTC"),
                "marketingOptIn": "true" if data.get("marketingOptIn") else "false",
                "report": "shadow-blueprint",
            }
        )
        return jsonify({"url": session.url})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/shadow/payment-success")
def shadow_payment_success():
    session_id = request.args.get("session_id")
    if not session_id:
        return render_template("shadow.html",
            auto_generate=False, chart_data="null", meta_data="null")
    try:
        import stripe as stripe_lib
        stripe_lib.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
        session = stripe_lib.checkout.Session.retrieve(session_id)
        if session.payment_status != "paid":
            return render_template("shadow.html",
                auto_generate=False, chart_data="null", meta_data="null")
        meta = session.metadata.to_dict()
        return render_template("shadow_thank_you.html",
            session_id=session_id,
            name=meta.get("name", ""),
            email=meta.get("email", ""))
    except Exception as e:
        print(f"Shadow payment success error: {e}")
        return render_template("shadow.html",
            auto_generate=False, chart_data="null", meta_data="null")


@app.route("/shadow/generate-after-payment", methods=["POST"])
def shadow_generate_after_payment():
    import stripe as stripe_lib
    stripe_lib.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
    data = request.json
    session_id = data.get("session_id", "")

    try:
        session = stripe_lib.checkout.Session.retrieve(session_id)
    except Exception as e:
        return jsonify({"error": f"Could not verify payment: {type(e).__name__}: {e}"}), 400

    if session.payment_status != "paid":
        return jsonify({"error": "Payment not complete."}), 400

    meta = session.metadata.to_dict()
    name = meta.get("name", "") or "the person"
    email = meta.get("email", "")
    date_str = meta.get("date", "")
    time_str = meta.get("time", "")
    city = meta.get("city", "")
    country = meta.get("country", "")
    tz_str = meta.get("tz", "UTC")
    marketing_opt_in = meta.get("marketingOptIn") == "true"

    try:
        lat = float(meta.get("lat", "0"))
        lng = float(meta.get("lng", "0"))
        year, month, day = [int(x) for x in date_str.split("-")]
        hour, minute = [int(x) for x in time_str.split(":")]
    except Exception as e:
        return jsonify({"error": f"Invalid birth data: {e}"}), 400

    try:
        chart = calculate_chart(name, year, month, day, hour, minute, lat, lng, tz_str)
    except Exception as e:
        return jsonify({"error": f"Chart calculation failed: {e}"}), 500

    birth_info = {"date": date_str, "time": time_str, "city": city, "country": country}
    log_customer(name=name, email=email, marketing_opt_in=marketing_opt_in,
                date=date_str, city=city, country=country, tag_name="shadow-blueprint")

    thread = threading.Thread(
        target=background_generate_and_send_shadow,
        args=(email, chart, birth_info),
        daemon=True
    )
    thread.start()

    preview_prompt = build_shadow_prompt(chart, birth_info, preview_only=True)
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

    def stream():
        chart_event = dict(chart)
        yield f"data: {json.dumps({'type':'chart','data':chart_event,'payload':{'name':name,'email':email,'date':date_str,'time':time_str,'city':city,'country':country,'lat':lat,'lng':lng,'tz':tz_str}})}\n\n"
        buffer = ""
        with client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            messages=[{"role":"user","content":preview_prompt}]
        ) as st:
            for text in st.text_stream:
                buffer += text
                if len(buffer) > 3:
                    flush = buffer[:-3]; buffer = buffer[-3:]
                    yield f"data: {json.dumps({'type':'text','content':clean_dashes(flush)})}\n\n"
        if buffer:
            yield f"data: {json.dumps({'type':'text','content':clean_dashes(buffer)})}\n\n"
        yield f"data: {json.dumps({'type':'done','email':email})}\n\n"

    return Response(stream(), mimetype="text/event-stream",
                   headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})


if __name__ == "__main__":
    import os as _os
    app.run(debug=False, port=int(_os.environ.get("PORT", 5001)))
