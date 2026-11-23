"""Reads a Czech car ad and finds what the seller admits is wrong with the car.

analyze(text) -> list of {"code", "sev", "quote"}:
  sev "critical"  the car is not a working, sellable car (not running, sold for parts, engine or
                  gearbox failure, head gasket, overheating, flood, no papers, crashed): drop it
  sev "serious"   a real defect that costs money (coolant or oil leak, smoke, clutch, flywheel,
                  turbo, DPF, injectors, warning lights, rust, ...): warn and lower the score
  sev "minor"     cosmetics, tyres, interior wear: shown as a note
  quote           the seller's own words around the match, as written in the ad

Negations ("nic neklepe", "bez koroze", "žádná kontrolka nesvítí") and finished repairs
("nové těsnění pod hlavou", "vyměněná spojka") do not count.
"""
import re
import unicodedata

W = r"(?:\w+ ){0,2}"  # up to two words in between ("ztráta chladicí kapaliny", "kape trochu olej")

RULES = [
    # --- critical: not a working, sellable car
    ("not_running", "critical", r"nepojizdn|\bnejede\b|\bnejezdi\b|nestartuje|nenastartuje|nelze nastartovat|motor nebezi|neni pojizdn|nechyta"),
    ("parts_or_repair", "critical", r"\bna (nahradni )?dily\b|\bna opravu\b|\bk oprave\b|pro (sikovn\w* )?kutil|rozprodam"),
    ("engine_failure", "critical", r"zavad\w* (na )?motor|vadn\w* motor|motor (je )?(ko|odesel|zadren\w*|na opravu|vadn\w*|nejde)\b|zadren\w* (motor|turb)|"
                                   r"prasknut\w* (blok|hlav)|motor klepe|klepe motor|ojnic\w* (klepe|ko)|nema kompres|ztrata kompres"),
    ("gearbox_failure", "critical", r"vadn\w* (prevodovk|dsg|automat)|(prevodovka|dsg|automat) (je )?(ko|vadn\w*|na opravu|zlobi|cuka|prokluzuje)\b|"
                                    r"nejde (za)?radit|vyskakuje (rychlost|kvalt)|neradi (rychlost|kvalt)"),
    ("head_gasket", "critical", r"(vadn\w*|odesl\w*|propal\w*|proraz\w*|praskl\w*|podezreni na) (\w+ )?tesneni pod hlav|"
                                r"tesneni pod hlavou (je )?(vadn|odesl|propal|proraz|na vymenu)|voda v oleji|olej ve vode|olej v chladic|majonez"),
    ("overheating", "critical", r"prehriva|prehral|prehrati|vari se (voda|chladic)"),
    ("flood", "critical", r"po povodni|vytopen|zatopen|byl\w* pod vodou"),
    ("crashed", "critical", r"havarovan|bouran|po havarii|po nehode|totalk|poskozen\w* (karoserie|predek|zadek|bok)"),
    ("no_papers", "critical", r"bez (tp|technick\w* prukaz|papiru|dokladu)\b|nelze prepsat|neni mozny prepis|odhlasen\w* z evidence"),
    ("finance_only_price", "critical", r"(cena|plati) (plati )?(pri|s|v pripade) (financovan|uver|leasing)|pri financovani|s financovanim|cena pri uveru|cena na uver|akontac"),
    ("lease", "critical", r"prevzeti leasingu|odkup leasingu|pronajem|operativni leasing"),
    # --- serious: a defect that costs money
    ("coolant_leak", "serious", r"(ztrat\w*|unik\w*|ubyva\w*|ubytek|uteka|tece|kape|chybi|chybel\w*|mizi|doplnovat|dolevat) " + W +
                                r"(chladici kapalin|nemrzn|antifriz|chladiv|vod[ayu]\b)|"
                                r"(chladici kapalin\w*|nemrznouc\w*|antifriz\w*) " + W + r"(ubyva|unika|uteka|tece|kape|mizi)|netesni chladic|tece z chladic"),
    ("oil_leak", "serious", r"(kape|tece|unika|poti se) " + W + r"olej|olej " + W + r"(kape|tece|unika)|olejov\w* (fleky|skvrn)|"
                            r"netesni (motor|prevodovk|vik)|zapocen\w* motor"),
    ("oil_consumption", "serious", r"(bere|zere|pali|spaluje) " + W + r"olej|spotreba oleje|ubyva olej"),
    ("smoke", "serious", r"\bkouri\b|modr\w* kour|bil\w* kour|cern\w* kour|\bdymi\b"),
    ("clutch", "serious", r"spojk\w* " + W + r"(prokluz|dosluhuj|na konci|na vymenu|hluc|cuka)|\bprokluzuje\b|vymenit spojk|nutn\w* (vymena )?spojk"),
    ("flywheel", "serious", r"(setrvacnik|dvouhmot\w*|dvojhmot\w*)\w* " + W + r"(hluc|klepe|vadn|na vymenu|dosluhuj|chrast)"),
    ("turbo", "serious", r"turb\w* " + W + r"(pisk|hviz|vadn|na vymenu|nefung|ma vuli|vule)|piskani turb"),
    ("dpf", "serious", r"(dpf|fap|filtr pevnych castic) " + W + r"(ucpan|zanesen|vadn|chyb|vyrezan|odstranen)|ucpan\w* (dpf|fap|filtr)|vyrezan\w* (dpf|fap)"),
    ("injectors", "serious", r"vstrikovac\w* " + W + r"(vadn|na vymenu|zlobi|nefung)|vadn\w* vstrikovac"),
    ("warning_lights", "serious", r"sviti " + W + r"kontrolk|kontrolka (motoru|airbag|abs|esp|oleje|dobijeni|zhaveni)|chyba motoru|check engine|"
                                  r"nouzov\w* rezim|\bnouzak|hlasi (chybu|zavadu)"),
    ("electrical", "serious", r"nedobiji|vybiji (se )?bateri|nefunguje " + W + r"(elektrik|centr|okna|stahovan|tachometr|palubn)|\bzkrat\b"),
    ("suspension_brakes", "serious", r"(vadn|hluc|vytluc|klepou)\w* " + W + r"(tlumic|lozisk|ramen|cep|silentblok|brzd|naprav)|"
                                     r"(tlumic\w*|lozisk\w*|ramen\w*|brzd\w*|naprav\w*) " + W + r"(vadn|hluc|klepou|klepe|na vymenu|vytluc)"),
    ("rust", "serious", r"\bkoroz\w*|\brez\b|rezav\w*|zreziv\w*|prorezl\w*|\bprorez\w*|\bhnij|diry (v|na) (prah|podlah|blatnik)"),
    ("no_stk", "serious", r"bez (platne )?stk\b|stk propadl\w*|propadl\w* stk|neprojde stk"),
    ("repair_needed", "serious", r"potrebuje " + W + r"(opravu|investic|vymenu)|nutn\w* (oprav|vymen|investic)|ceka na opravu|bude potreba (vymenit|opravit)"),
    ("mileage_doubt", "serious", r"km (nelze|nemohu|nemuzu) (doloz|garant)|neznam\w* (najezd|km)|tachometr (nefung|vymenen)|stocen\w* (tachometr|km)"),
    # --- minor: cosmetics and wear
    ("body_cosmetic", "minor", r"tukan|\btukl|odren|skrabanc|oderk|promackl|\bdent\b|lak " + W + r"(oprysk|olupuj|loupe|vybledl)|vybledl\w* lak|kaminkov"),
    ("glass", "minor", r"praskl\w* (celni )?sklo|prasklin\w* (na|ve) (celnim )?skle|kaminek (v|na) skle"),
    ("tyres", "minor", r"(pneu|pneumatik\w*) " + W + r"(na vymenu|ojete|sjete|na hranici)|nutno koupit pneu"),
    ("interior_wear", "minor", r"opotreben\w* (interier|sedadl|volant)|propalen\w* (sedadl|potah)|zapach|smrdi|kourilo se|\bkurak\b|kurack"),
    ("ac", "minor", r"(klima|klimatizace) " + W + r"(nefung|nechladi|vadn|na doplneni|nutno doplnit)"),
]
RULES_RE = [(code, sev, re.compile(rx)) for code, sev, rx in RULES]
SEV_RANK = {"minor": 1, "serious": 2, "critical": 3}

NEGATION_BEFORE = re.compile(r"(\bne|nikdy|\bbez|nebyl[oa]?|nebyly|zadn[eyao]?\w*|neni|nemel[oa]?|nic|nikde|neni zadna)\s*$")
REPAIRED_BEFORE = re.compile(r"(\bnov\w*|vymenen\w*|vymenil\w*|vymena|opraven\w*|opravil\w*|udelan\w*|po vymene|po oprave|"
                             r"zrenovovan\w*|repasovan\w*|vcetne)\s+(\w+\s+){0,3}$")
REPAIRED_AFTER = re.compile(r"^\W{0,3}(\w+\s+){0,2}(vymenen|vymeneno|vymenena|opraven|opraveno|opravena|nov[aey]|v poradku|vyreseno)")
MINOR_RUST = re.compile(r"(drobn\w*|mal[aey]\w*|minimaln\w*|lehk\w*|povrchov\w*|kosmetick\w*|pocinajic\w*|zacinajic\w*|mistama|misty)\s+(\w+\s+)?$")
# Questions to the buyer and general disclaimers, not facts about the car.
DISCLAIMER = re.compile(r"(nabizim|nabidnu|vykupuji|vykoupim|koupim|\bvykup\w*|vymena mozna|protiucet)\b[^.!?]{0,35}$")


def normalize_with_map(text):
    """Lowercase, strip diacritics, collapse whitespace. Returns (normalized, index map into text)."""
    out, idx = [], []
    for i, ch in enumerate(text or ""):
        if ch.isspace() or ch == "\xa0":
            if out and out[-1] == " ":
                continue
            out.append(" ")
            idx.append(i)
            continue
        for b in unicodedata.normalize("NFKD", ch):
            if unicodedata.combining(b):
                continue
            out.append(b.lower())
            idx.append(i)
    return "".join(out), idx


def _quote(text, idx, start, end, pad=45):
    a = idx[max(0, start - pad)] if idx else 0
    b = idx[min(len(idx) - 1, end + pad)] + 1 if idx else 0
    q = re.sub(r"\s+", " ", text[a:b]).strip()
    # cut to word boundaries
    if a > 0:
        q = q.split(" ", 1)[-1] if " " in q else q
        q = "…" + q
    if b < len(text):
        q = (q.rsplit(" ", 1)[0] if " " in q else q) + "…"
    return q


def analyze(text):
    t, idx = normalize_with_map(text)
    found = {}
    for code, sev, rx in RULES_RE:
        for m in rx.finditer(t):
            before = t[max(0, m.start() - 40):m.start()]
            after = t[m.end():m.end() + 30]
            if t[max(0, m.start() - 2):m.start()] == "ne" or NEGATION_BEFORE.search(before):
                continue
            if REPAIRED_BEFORE.search(before) or REPAIRED_AFTER.search(after):
                continue
            if DISCLAIMER.search(before):
                continue
            s = sev
            if code == "rust" and MINOR_RUST.search(before):
                s = "minor"
            prev = found.get(code)
            if prev and SEV_RANK[prev["sev"]] >= SEV_RANK[s]:
                continue
            found[code] = {"code": code, "sev": s, "quote": _quote(text, idx, m.start(), m.end())}
    return sorted(found.values(), key=lambda d: -SEV_RANK[d["sev"]])


def worst(defects):
    return max((SEV_RANK[d["sev"]] for d in defects), default=0)
