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
    # Houses 1, 2, 3, 6, 10, 11 are the most relevant for purpose/career/values/brand.
    hr = {}
    for h in [1, 2, 3, 6, 10, 11]:
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
    # Em-dash with spaces -> comma + space
    text = re.sub(r'\s*,\s*', ', ', text)
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
    """v2 design - bold, editorial, on-brand."""
    return f"""<!DOCTYPE html>
<html><head>
<meta charset="UTF-8">
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,400;0,500;0,700;1,400;1,500;1,700&family=Caveat:wght@400;500;600&display=swap" rel="stylesheet">
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
      <p style="margin:0 0 0;font-family:'Caveat',cursive;font-size:32px;color:#AA3157;line-height:1;">Lena</p>
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

    cells_html = "".join([
        f'<td><div class="cell-label">{label}</div><div class="cell-value">{value}</div><div class="cell-house">{house}</div></td>'
        for label, value, house in cells
    ])

    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<style>
  @import url('https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,400;0,500;0,700;1,400;1,500;1,700&family=Caveat:wght@400;500;600&display=swap');
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

  .chart-table {{
    width: 100%;
    border-collapse: collapse;
    border: 2px solid #1E1E1E;
    margin: 0 0 40px;
  }}

  .chart-table td {{
    background: #EFEBEA;
    padding: 10px 4px;
    text-align: center;
    border: 1px solid #1E1E1E;
    width: 20%;
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
  </div>

  <p class="chart-strip-heading">Your Chart at a Glance</p>
  <table class="chart-table">
    <tr>{cells_html}</tr>
  </table>

  <div class="report">
    {report_body}
  </div>

  <div class="footer">
    <div class="footer-label">✦ Lunabylena.com ✦</div>
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


def add_to_kit(name, email):
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

        # Step 2: Get or create the purpose-blueprint tag
        tags_resp = req.get(f"{base}/tags", headers=headers, timeout=10)
        tag_id = None
        for tag in tags_resp.json().get("tags", []):
            if tag.get("name") == "purpose-blueprint":
                tag_id = tag["id"]
                break

        if not tag_id:
            create_resp = req.post(
                f"{base}/tags",
                json={"name": "purpose-blueprint"},
                headers=headers,
                timeout=10
            )
            tag_id = create_resp.json().get("tag", {}).get("id")

        if not tag_id:
            print("Kit: subscriber added but could not get/create tag")
            return True

        # Step 3: Tag the subscriber
        tag_resp = req.post(
            f"{base}/subscribers/{subscriber_id}/tags/{tag_id}",
            headers=headers,
            timeout=10
        )

        if tag_resp.status_code in (200, 201):
            print(f"Kit: added {email} with tag purpose-blueprint (id {subscriber_id})")
        else:
            print(f"Kit: subscriber added but tagging failed: {tag_resp.status_code} {tag_resp.text[:200]}")
        return True

    except Exception as e:
        print(f"Kit: error adding subscriber: {e}")
        return False

def log_customer(name, email, marketing_opt_in, date, city, country):
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
        add_to_kit(name=name, email=email)


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
