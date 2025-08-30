import re

"""
it has to be a network watcher. see https://app.apollo.io/#/login
and https://nowsecure.nl

i think it has to do with `cf-chl-out` and `cf-ch-out-s` response headers

`cf_clearance` is the cookie that if it gets set, it bypasses the challenge.

but https://nowsecure.nl does some weird network crap where 
it says it sets good cookies, but actually doesn't?
"""


CHECKBOX_MARKERS: list[re.Pattern] = [
    # TODO: the cdn-cgi... marker needs to be a 
    # network watcher rather than a DOM watcher
    re.compile(r"/cdn-cgi/challenge-platform/h/", re.IGNORECASE),
    re.compile(r"<title>Just a moment\.\.\.</title>", re.IGNORECASE),
]

def should_wait(html: str):
    """quick heuristic: does the html look like a cloudflare waiting / challenge page?

    returns True if we detect well-known marker strings that usually show up while
    a browser is being presented the *"just a moment"* interstitial (e.g. hcaptcha / turnstile / js challenge).

    :param html: raw html of the page we just loaded.
    :return: bool indicating a probable cf challenge state.
    :rtype: bool
    """
    return any(marker.search(html) for marker in CHECKBOX_MARKERS)