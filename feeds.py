"""
The source allowlist. This file IS your editorial policy.

You cannot publish garbage you never ingested, which makes this the highest-leverage
file in the whole pipeline. Reviewing sources once beats reviewing stories daily.

tier 1 = wires, journals, space/health agencies. Authoritative on their own.
tier 2 = established specialist outlets. Solid, but prefer corroboration.
tier 3 = solutions-journalism and NGO outlets. Useful leads, NEVER sufficient alone.

A story needs >=2 independent sources, at least one of them tier 1 or 2, to publish.

IMPORTANT: run `python gud_pipeline.py check-feeds` before your first real run.
Feed URLs rot. Some of these will need fixing, and that's normal maintenance.
Adding/pruning feeds is roughly your entire ongoing editorial job.
"""

FEEDS = [
    # ---------------- tier 1: agencies, journals, wires ----------------
    ("NASA",              "https://www.nasa.gov/news-release/feed/",                        1, "space"),
    ("ESA",               "https://www.esa.int/rssfeed/Our_Activities/Space_News",          1, "space"),
    ("Nature News",       "https://www.nature.com/nature.rss",                              1, "discovery"),
    ("Science Daily",     "https://www.sciencedaily.com/rss/top/science.xml",               1, "discovery"),
    ("WHO News",          "https://www.who.int/rss-feeds/news-english.xml",                 1, "discovery"),
    ("Phys.org Earth",    "https://phys.org/rss-feed/earth-news/",                          1, "nature"),

    # ---------------- tier 2: established specialists ----------------
    ("Mongabay",          "https://news.mongabay.com/feed/",                                2, "nature"),
    ("Carbon Brief",      "https://www.carbonbrief.org/feed/",                              2, "progress"),
    ("The Conversation",  "https://theconversation.com/uk/environment/articles.atom",       2, "progress"),
    ("New Scientist",     "https://www.newscientist.com/feed/home/",                        2, "discovery"),
    ("Ars Technica Sci",  "https://feeds.arstechnica.com/arstechnica/science",              2, "discovery"),
    ("Smithsonian",       "https://www.smithsonianmag.com/rss/latest_articles/",            2, "discovery"),
    ("BBC Science",       "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml",  2, "discovery"),
    ("Guardian Environ",  "https://www.theguardian.com/environment/rss",                    2, "nature"),
    ("Guardian Science",  "https://www.theguardian.com/science/rss",                        2, "discovery"),
    ("IUCN",              "https://iucn.org/rss.xml",                                       2, "animals"),

    # ---------------- tier 3: solutions journalism, NGOs ----------------
    ("Reasons to be Cheerful", "https://reasonstobecheerful.world/feed/",                    3, "kindness"),
    ("Positive News",     "https://www.positive.news/feed/",                                3, "kindness"),
    ("Good News Network", "https://www.goodnewsnetwork.org/feed/",                           3, "kindness"),
    ("Optimist Daily",    "https://www.optimistdaily.com/feed/",                             3, "kindness"),

    # --- added for corroboration: mainstream outlets covering the same big stories ---
    ("BBC Health",        "https://feeds.bbci.co.uk/news/health/rss.xml",                   2, "discovery"),
    ("Guardian World",    "https://www.theguardian.com/world/rss",                          2, "progress"),
    ("Independent Sci",   "https://www.independent.co.uk/news/science/rss",                 2, "discovery"),
    ("CBS News Sci",      "https://www.cbsnews.com/latest/rss/science",                     2, "discovery"),
    ("NPR Science",       "https://feeds.npr.org/1007/rss.xml",                             2, "discovery"),
]

# Clusters must match the app's onboarding options exactly.
CLUSTERS = ["nature", "discovery", "kindness", "progress", "animals", "space"]

# Illustration scenes available in the app. The pipeline picks one per story;
# no photographs are ever used, which keeps you clear of image licensing entirely.
SCENES = {
    "nature":    ["kakapo", "sun", "whale"],
    "animals":   ["whale", "kakapo"],
    "space":     ["ozone", "sun"],
    "discovery": ["vial", "ozone"],
    "progress":  ["sun", "ozone", "vial"],
    "kindness":  ["library", "sun"],
}

# Keyword hints that override the cluster default, so illustrations feel deliberate.
SCENE_HINTS = [
    # Checked in order — first match wins, so put the most specific first.
    (("whale", "ocean", "coral", "reef", "marine", "sea ", "fish", "shark"),      "whale"),
    (("beaver", "wetland", "river", "pond", "stream", "brook", "flood", "drought",
      "water", "wildlife", "bird", "parrot", "species", "forest", "tree", "rewild",
      "habitat", "conservation", "nature", "wolf", "bee", "insect"),             "kakapo"),
    (("vaccine", "trial", "drug", "cancer", "patient", "malaria", "brain",
      "alzheimer", "disease", "cells", "molecule", "protein", "gene", "therapy",
      "obesity", "cognitive", "neuro", "blood", "immune", "chemical", "hydrogen",
      "plastic", "carbon", "material", "battery", "chemistry", "lab"),           "vial"),
    (("solar", "wind", "renewable", "grid", "energy", "power", "electric"),      "sun"),
    (("ozone", "satellite", "orbit", "telescope", "atmosphere", "space", "star",
      "planet", "galaxy", "moon", "mars", "nasa"),                               "ozone"),
    (("library", "school", "book", "community", "volunteer", "neighbour",
      "charity", "donat", "kindness", "student", "teacher"),                     "library"),
]
