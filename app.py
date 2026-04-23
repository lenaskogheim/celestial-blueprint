import os, json, warnings
warnings.filterwarnings("ignore")
from flask import Flask, request, jsonify, Response, render_template
from kerykeion.astrological_subject_factory import AstrologicalSubjectFactory
from kerykeion.aspects import AspectsFactory
import anthropic

app = Flask(__name__)

ALL_SIGNS = ["Ari","Tau","Gem","Can","Leo","Vir","Lib","Sco","Sag","Cap","Aqu","Pis"]
SIGN_NAMES = {"Ari":"Aries","Tau":"Taurus","Gem":"Gemini","Can":"Cancer","Leo":"Leo","Vir":"Virgo","Lib":"Libra","Sco":"Scorpio","Sag":"Sagittarius","Cap":"Capricorn","Aqu":"Aquarius","Pis":"Pisces"}
RULERS = {"Ari":"Mars","Tau":"Venus","Gem":"Mercury","Can":"Moon","Leo":"Sun","Vir":"Mercury","Lib":"Venus","Sco":"Mars","Sag":"Jupiter","Cap":"Saturn","Aqu":"Uranus","Pis":"Neptune"}
HOUSE_NAMES = {"First_House":"1st","Second_House":"2nd","Third_House":"3rd","Fourth_House":"4th","Fifth_House":"5th","Sixth_House":"6th","Seventh_House":"7th","Eighth_House":"8th","Ninth_House":"9th","Tenth_House":"10th","Eleventh_House":"11th","Twelfth_House":"12th"}

def get_coordinates(city, country):
    try:
        from geopy.geocoders import Nominatim
        from timezonefinder import TimezoneFinder
        geolocator = Nominatim(user_agent="astro_app", timeout=10)
        location = geolocator.geocode(f"{city}, {country}", timeout=10)
        if location:
            tf = TimezoneFinder()
            tz = tf.timezone_at(lat=location.latitude, lng=location.longitude)
            return location.latitude, location.longitude, tz
    except Exception:
        pass
    fallback = {
        "oslo": (59.9139, 10.7522, "Europe/Oslo"),
        "bergen": (60.3913, 5.3221, "Europe/Oslo"),
        "voss": (60.6281, 6.4148, "Europe/Oslo"),
        "trondheim": (63.4305, 10.3951, "Europe/Oslo"),
        "stavanger": (58.9700, 5.7331, "Europe/Oslo"),
        "førde": (61.4510, 5.8573, "Europe/Oslo"),
        "forde": (61.4510, 5.8573, "Europe/Oslo"),
        "ålesund": (62.4722, 6.1549, "Europe/Oslo"),
        "kristiansand": (58.1599, 8.0182, "Europe/Oslo"),
        "tromsø": (69.6489, 18.9551, "Europe/Oslo"),
        "london": (51.5074, -0.1278, "Europe/London"),
        "paris": (48.8566, 2.3522, "Europe/Paris"),
        "berlin": (52.5200, 13.4050, "Europe/Berlin"),
        "madrid": (40.4168, -3.7038, "Europe/Madrid"),
        "malaga": (36.7213, -4.4213, "Europe/Madrid"),
        "málaga": (36.7213, -4.4213, "Europe/Madrid"),
        "barcelona": (41.3851, 2.1734, "Europe/Madrid"),
        "rome": (41.9028, 12.4964, "Europe/Rome"),
        "amsterdam": (52.3676, 4.9041, "Europe/Amsterdam"),
        "havana": (23.1136, -82.3666, "America/Havana"),
        "new york": (40.7128, -74.0060, "America/New_York"),
        "los angeles": (34.0522, -118.2437, "America/Los_Angeles"),
        "miami": (25.7617, -80.1918, "America/New_York"),
        "chicago": (41.8781, -87.6298, "America/Chicago"),
        "toronto": (43.6532, -79.3832, "America/Toronto"),
        "sydney": (-33.8688, 151.2093, "Australia/Sydney"),
        "melbourne": (-37.8136, 144.9631, "Australia/Melbourne"),
        "tokyo": (35.6762, 139.6503, "Asia/Tokyo"),
        "dubai": (25.2048, 55.2708, "Asia/Dubai"),
        "stockholm": (59.3293, 18.0686, "Europe/Stockholm"),
        "copenhagen": (55.6761, 12.5683, "Europe/Copenhagen"),
        "helsinki": (60.1699, 24.9384, "Europe/Helsinki"),
        "reykjavik": (64.1355, -21.8954, "Atlantic/Reykjavik"),
        "lisbon": (38.7169, -9.1395, "Europe/Lisbon"),
        "athens": (37.9838, 23.7275, "Europe/Athens"),
        "warsaw": (52.2297, 21.0122, "Europe/Warsaw"),
        "budapest": (47.4979, 19.0402, "Europe/Budapest"),
        "vienna": (48.2082, 16.3738, "Europe/Vienna"),
        "zurich": (47.3769, 8.5417, "Europe/Zurich"),
        "brussels": (50.8503, 4.3517, "Europe/Brussels"),
        "prague": (50.0755, 14.4378, "Europe/Prague"),
    }
    key = city.lower().strip()
    if key in fallback:
        return fallback[key]
    return None, None, None

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/generate", methods=["POST"])
def generate():
    data = request.json
    name = data.get("name","").strip() or "the person"
    date_str = data.get("date","")
    time_str = data.get("time","")
    city = data.get("city","")
    country = data.get("country","")

    year, month, day = [int(x) for x in date_str.split("-")]
    hour, minute = [int(x) for x in time_str.split(":")]

    # Use pre-resolved coordinates from frontend if available
    lat = data.get("lat")
    lng = data.get("lng")
    tz_str = data.get("tz")

    if not lat or not lng or not tz_str:
        # Fallback to server-side lookup
        lat, lng, tz_str = get_coordinates(city, country)
        if lat is None:
            return jsonify({"error": f"Could not find coordinates for {city}, {country}. Please try a nearby major city."}), 400

    lat = float(lat)
    lng = float(lng)

    try:
        s = AstrologicalSubjectFactory.from_birth_data(
            name=name, year=year, month=month, day=day, hour=hour, minute=minute,
            lng=lng, lat=lat, tz_str=tz_str,
            zodiac_type="Tropical", houses_system_identifier="W",
            online=False, suppress_geonames_warning=True
        )

        # Whole Sign houses: each sign = one house, starting from ASC sign
        asc_sign = s.first_house.sign
        asc_idx = ALL_SIGNS.index(asc_sign)
        ws_houses = [ALL_SIGNS[(asc_idx+i)%12] for i in range(12)]

        def hn(h): return HOUSE_NAMES.get(h,h)
        def fs(a): return SIGN_NAMES.get(a,a)

        # Planets
        planets_raw = {
            "Sun":s.sun,"Moon":s.moon,"Mercury":s.mercury,"Venus":s.venus,
            "Mars":s.mars,"Jupiter":s.jupiter,"Saturn":s.saturn,
            "Uranus":s.uranus,"Neptune":s.neptune,"Pluto":s.pluto
        }
        pd = {pn:{"sign":fs(p.sign),"house":hn(p.house),"position":round(p.position,2),"abs_pos":round(p.abs_pos,2)} for pn,p in planets_raw.items()}

        nn = s.true_north_lunar_node
        sn = s.true_south_lunar_node
        pd["North Node"] = {"sign":fs(nn.sign),"house":hn(nn.house),"position":round(nn.position,2),"abs_pos":round(nn.abs_pos,2)}
        pd["South Node"] = {"sign":fs(sn.sign),"house":hn(sn.house),"position":round(sn.position,2),"abs_pos":round(sn.abs_pos,2)}
        pd["Chiron"] = {"sign":fs(s.chiron.sign),"house":hn(s.chiron.house),"position":round(s.chiron.position,2),"abs_pos":round(s.chiron.abs_pos,2)}

        # Part of Fortune
        pof = (s.first_house.abs_pos + s.moon.abs_pos - s.sun.abs_pos) % 360
        pof_sign_abbr = ALL_SIGNS[int(pof//30)]
        pof_deg = round(pof%30, 2)
        pof_house = ws_houses.index(pof_sign_abbr)+1 if pof_sign_abbr in ws_houses else "?"

        # Angles — use medium_coeli and imum_coeli for true MC/IC degrees
        mc = s.medium_coeli
        ic = s.imum_coeli
        mc_sign_abbr = mc.sign
        ic_sign_abbr = ic.sign
        mc_ws_house = ws_houses.index(mc_sign_abbr) + 1 if mc_sign_abbr in ws_houses else "?"
        ic_ws_house = ws_houses.index(ic_sign_abbr) + 1 if ic_sign_abbr in ws_houses else "?"

        # DC sign from seventh house abs_pos
        dc_sign_abbr = ALL_SIGNS[int(s.seventh_house.abs_pos // 30)]

        angles = {
            "ASC": {"sign": fs(asc_sign), "position": round(s.first_house.position, 2)},
            "MC":  {"sign": fs(mc_sign_abbr), "position": round(mc.position, 2), "ws_house": mc_ws_house},
            "IC":  {"sign": fs(ic_sign_abbr), "position": round(ic.position, 2), "ws_house": ic_ws_house},
            "DC":  {"sign": fs(dc_sign_abbr), "position": round(s.seventh_house.position, 2)},
        }

        # House rulers for key houses
        hr = {h: {"sign": fs(ws_houses[h-1]), "ruler": RULERS[ws_houses[h-1]]} for h in [1,2,3,6,10,11]}

        # Aspects
        result = AspectsFactory.single_chart_aspects(s)
        career = {"Sun","Moon","Mercury","Venus","Mars","Jupiter","Saturn",
                  "True_North_Lunar_Node","True_South_Lunar_Node",
                  "Ascendant","Medium_Coeli","Imum_Coeli","Chiron"}
        aspects = []
        seen = set()
        for a in result.aspects:
            p1,p2 = a.p1_name, a.p2_name
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
        aspects.sort(key=lambda x: x["orb"])

        chart = {
            "name": name,
            "planets": pd,
            "angles": angles,
            "house_rulers": hr,
            "ws_houses": [fs(s) for s in ws_houses],
            "part_of_fortune": {"sign": fs(pof_sign_abbr), "degree": pof_deg, "house": pof_house},
            "aspects": aspects
        }

        # Build prompt
        planet_lines = [f"  - {n}: {d['sign']}, {d['house']} house, {d['position']}°" for n,d in pd.items()]
        aspect_lines = [f"  - {a['p1']} {a['aspect']} {a['p2']} (orb: {a['orb']}°)" for a in aspects[:25]]
        ruler_lines = [
            f"  - 1st house ({hr[1]['sign']}) ruler: {hr[1]['ruler']} — in {pd.get(hr[1]['ruler'],{}).get('sign','?')} {pd.get(hr[1]['ruler'],{}).get('house','?')} house",
            f"  - 2nd house ({hr[2]['sign']}) ruler: {hr[2]['ruler']} — in {pd.get(hr[2]['ruler'],{}).get('sign','?')} {pd.get(hr[2]['ruler'],{}).get('house','?')} house",
            f"  - 3rd house ({hr[3]['sign']}) ruler: {hr[3]['ruler']} — in {pd.get(hr[3]['ruler'],{}).get('sign','?')} {pd.get(hr[3]['ruler'],{}).get('house','?')} house",
            f"  - 6th house ({hr[6]['sign']}) ruler: {hr[6]['ruler']} — in {pd.get(hr[6]['ruler'],{}).get('sign','?')} {pd.get(hr[6]['ruler'],{}).get('house','?')} house",
            f"  - 10th house ({hr[10]['sign']}) ruler: {hr[10]['ruler']} — in {pd.get(hr[10]['ruler'],{}).get('sign','?')} {pd.get(hr[10]['ruler'],{}).get('house','?')} house",
            f"  - 11th house ({hr[11]['sign']}) ruler: {hr[11]['ruler']} — in {pd.get(hr[11]['ruler'],{}).get('sign','?')} {pd.get(hr[11]['ruler'],{}).get('house','?')} house",
        ]

        prompt = f"""You are a professional astrologer writing a premium, deeply personal Life Purpose, Career & Business Blueprint Report. Your tone is warm, wise, and direct. You speak in second person ("you"). You translate astrology into lived human experience — no jargon, only meaning. Every sentence must feel specific to this person, never generic. This is a paid premium report — make every section rich, detailed, and worth every penny.

BIRTH DETAILS:
  Name: {name}
  Date: {date_str}, Time: {time_str}
  Place: {city}, {country}
  House System: Whole Sign (each sign = one full house, starting from ASC sign)

PLANETS & WHOLE SIGN HOUSES:
{chr(10).join(planet_lines)}

ANGLES:
  - ASC (Rising): {angles['ASC']['sign']} {angles['ASC']['position']}°
  - MC: {angles['MC']['sign']} {angles['MC']['position']}° — falls in Whole Sign house {angles['MC']['ws_house']}
  - IC: {angles['IC']['sign']} {angles['IC']['position']}° — falls in Whole Sign house {angles['IC']['ws_house']}
  - DC: {angles['DC']['sign']} {angles['DC']['position']}°

WHOLE SIGN HOUSE SEQUENCE (1st through 12th):
  {', '.join(chart['ws_houses'])}

KEY HOUSE RULERS:
{chr(10).join(ruler_lines)}

PART OF FORTUNE: {chart['part_of_fortune']['sign']} {chart['part_of_fortune']['degree']}° in house {chart['part_of_fortune']['house']}

KEY ASPECTS (sorted by tightest orb — smallest orb = most important):
{chr(10).join(aspect_lines)}

---

Write the report using EXACTLY these eight sections with ## headers. Each section must be detailed and rich — minimum 3-4 paragraphs per section for the longer sections. Do not rush. Go deep.

## Your Soul's Signature
4-5 sentences. A powerful, poetic portrait of who this person is at their core. Weave together Sun, Moon, ASC, and the 2-3 tightest aspects. Make it feel like the most accurate thing anyone has ever said about them.

## Your IC — Where You Come From
3 paragraphs. Interpret the IC sign and which Whole Sign house it falls in. Describe the emotional foundation, early home environment, and what was inherited — both as gift and as wound. Then describe the full IC-to-MC axis as the defining story arc of their life. Include any planets or aspects conjunct the IC or MC and what they add to this story.

## Your Life Purpose
4 paragraphs. Interpret the North and South Node fully — signs, houses, and what the axis reveals about the soul's evolutionary direction. What familiar pattern must they move away from (South Node)? What new territory must they move toward (North Node)? Include aspects to the nodes and what they add. Make this feel like the deepest truth of why this person is alive.

## Your Career Path & Calling
5 paragraphs — go deep into each:
1. The 10th Whole Sign house: sign on the cusp, any planets inside, what the vocation looks and feels like
2. The 6th house: how they work best day to day, what daily work environment suits them
3. The 2nd house: their relationship with money, values, and what they need to feel secure
4. The career ruler (ruler of the 10th house sign): where it sits, its aspects, what this reveals about where career energy flows
5. The MC sign and house it falls in: what their public reputation and legacy will be built on
Include at least 5-6 specific real-world career examples that genuinely fit this chart.

## Your Unique Gifts
3 paragraphs. What does this person have that others don't? Draw on: benefic aspects to personal planets, Moon placement and aspects, 9th house, Chiron as wound-become-gift, Part of Fortune, Venus and Jupiter aspects, any stelliums. Be specific — name the gift and explain exactly where it comes from in the chart.

## Your Greatest Challenge
2-3 paragraphs. Draw on difficult aspects (squares, oppositions under 5° orb), Saturn placement and aspects, South Node shadow patterns, 12th house planets. Frame every challenge as an invitation — what is this tension asking them to integrate? What becomes possible on the other side of it?

## Your Business & Personal Brand Blueprint
This is the section on building a business, personal brand, and social media presence that is aligned with this person's chart. Be highly specific and practical. Cover all of these in rich paragraphs:

1. Brand Identity & Positioning (2 paragraphs): What should their brand look and feel like? What energy, aesthetic, and message should they lead with? Draw on ASC sign, 10th house sign, Venus sign and house, and any planets in the 1st house.

2. Content Style & Communication (2 paragraphs): How should they show up online? What content format suits them best — video, writing, speaking, visual, educational, storytelling, behind-the-scenes? What topics give them natural authority? Draw on Mercury sign and house, 3rd house sign and ruler, Moon sign and house.

3. Audience & Community Growth (2 paragraphs): Who is their natural audience and how do they attract followers? How do they build a loyal community? What makes people come back? Draw on 11th house sign and ruler, Jupiter placement, North Node, and any planets in the 11th house.

4. Monetisation & Income Streams (1-2 paragraphs): How does money flow most naturally to them? What income models fit their chart — products, services, courses, 1:1, content, affiliate? Draw on 2nd house ruler and placement, 8th house, Venus aspects.

5. Social Media Platform Fit (1 paragraph): Based on their chart, which platforms suit them best and why? Consider: Instagram (Venus/visual), TikTok (Moon/viral/emotional), YouTube (Sun/long form/authority), Podcast/writing (Mercury/3rd house), LinkedIn (Saturn/10th house professional).

## A Message From Your Chart
1 single powerful paragraph. End the entire report with a direct, personal, luminous message to this specific person. Reference the single most exact aspect in their entire chart (smallest orb). Write as if the chart itself is speaking to them. This should be the most memorable thing they read — the paragraph they screenshot and save.

---

CRITICAL RULES:
- Use Whole Sign houses throughout. The MC and IC fall in whichever Whole Sign house their degree lands in — state this explicitly.
- The tightest aspects (smallest orb) carry the most interpretive weight. Always prioritise them.
- Never write anything that could apply to anyone. Every sentence must be grounded in a specific placement or aspect from this chart.
- This report should feel like it took an expert astrologer hours to write. Make it worth paying for."""

        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY",""))

        def stream():
            yield f"data: {json.dumps({'type':'chart','data':chart})}\n\n"
            with client.messages.stream(
                model="claude-haiku-4-5-20251001",
                max_tokens=8000,
                messages=[{"role":"user","content":prompt}]
            ) as st:
                for text in st.text_stream:
                    yield f"data: {json.dumps({'type':'text','content':text})}\n\n"
            yield f"data: {json.dumps({'type':'done'})}\n\n"

        return Response(stream(), mimetype="text/event-stream",
                       headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(debug=False, port=5000)
