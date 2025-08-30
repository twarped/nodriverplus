import urlcanon

def fix_url(url: str):
    """make the url more like how chrome would make it

    :param url: raw url.
    :return: fixed url
    :rtype: str
    """
    return urlcanon.google.canonicalize(url).__str__()
