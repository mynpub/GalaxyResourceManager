"""Provisional Earth crust model; quantities and weights are integer-only.

Hand-rounded provisional game approximation informed by the abundance figure in
USGS Fact Sheet 087-02,
https://pubs.usgs.gov/fs/2002/fs087-02/ . These are gameplay weights, not ore grades
or recoverability estimates, and are not a verbatim reference dataset.
Weights use parts per billion, including approximate
gold (0.004 ppm) and silver (0.075 ppm). Unlisted elements
(including synthetic elements) have zero weight. Edit ABUNDANCE to configure.
"""

SYMBOLS = tuple("""
H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni Cu Zn
Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe Cs Ba La Ce
Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir Pt Au Hg Tl Pb Bi Po At
Rn Fr Ra Ac Th Pa U Np Pu Am Cm Bk Cf Es Fm Md No Lr Rf Db Sg Bh Hs Mt Ds Rg
Cn Nh Fl Mc Lv Ts Og
""".split())

ABUNDANCE = dict.fromkeys(SYMBOLS, 0)
ABUNDANCE.update({
    "O": 461000, "Si": 282000, "Al": 82300, "Fe": 56300, "Ca": 41500,
    "Na": 23600, "Mg": 23300, "K": 20900, "Ti": 5650, "H": 1400,
    "P": 1050, "Mn": 950, "F": 585, "Ba": 425, "Sr": 370, "S": 350,
    "C": 200, "Zr": 165, "Cl": 145, "V": 120, "Cr": 102, "Rb": 90,
    "Ni": 84, "Zn": 70, "Ce": 67, "Cu": 60, "Y": 33, "La": 39,
    "Nd": 42, "Co": 25, "Sc": 22, "Li": 20, "Nb": 20, "Ga": 19,
    "Pb": 14, "B": 10, "Th": 10, "Pr": 9, "Sm": 7, "Gd": 6,
    "Dy": 5, "Yb": 3, "Er": 3, "U": 3, "Be": 3, "Hf": 3,
    "Sn": 2, "Ta": 2, "As": 2, "W": 1, "Mo": 1, "Ge": 1,
    "N": 19, "Cs": 3, "Eu": 2, "Tb": 1, "Ho": 1, "Tm": 1, "Lu": 1,
})
ABUNDANCE = {symbol: ppm * 1000 for symbol, ppm in ABUNDANCE.items()}
ABUNDANCE.update({"Au": 4, "Ag": 75})
SOURCE = {
    "title": "USGS Fact Sheet 087-02: Rare Earth Elements—Critical Resources for High Technology",
    "url": "https://pubs.usgs.gov/fs/2002/fs087-02/",
    "model": "Hand-rounded provisional game approximation, informed by the USGS abundance figure; not verbatim reference values. Configurable ppb weights normalized at runtime.",
}

MINING_LEVEL_99_SECONDS = 631152000  # 20 Julian years.
MAX_LEVEL = 999
XP_THRESHOLDS = tuple(
    (MINING_LEVEL_99_SECONDS * (level - 1) ** 2 + 98 ** 2 - 1) // 98 ** 2
    for level in range(1, MAX_LEVEL + 1)
)


def level(xp):
    """Levels 1..999; curve is configurable, with no production scaling."""
    from bisect import bisect_right
    return max(1, min(MAX_LEVEL, bisect_right(XP_THRESHOLDS, xp)))
