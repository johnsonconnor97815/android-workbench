"""Pure Google Play UI recognition. No device access or model execution."""

from dataclasses import asdict, dataclass
import re
from urllib.parse import parse_qsl, urlsplit
import xml.etree.ElementTree as ET

PLAY = "com.android.vending"
LABELS = ("install", "安装", "安裝", "インストール", "설치", "installer",
          "installieren", "instalar", "installa", "установить")
SHEET = PLAY + "/com.google.android.finsky.transparentmainactivity.TransparentMainActivityPrivate"
DETAILS = PLAY + "/com.google.android.finsky.activities.MainActivityPrivate"


class UiError(ValueError):
    pass


def normalized(value):
    return " ".join(value.casefold().split())


def bounds(node):
    match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
    if match:
        box = tuple(map(int, match.groups()))
        if box[0] < box[2] and box[1] < box[3]:
            return box
    return None


def values(node):
    return {normalized(line) for field in ("text", "content-desc")
            for line in node.get(field, "").splitlines() if line.strip()}


def parse_ui(xml):
    if len(xml.encode()) > 4 * 1024 * 1024:
        raise UiError("UI hierarchy exceeds the evidence limit")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as error:
        raise UiError("Invalid UI hierarchy") from error
    if root.tag != "hierarchy":
        raise UiError("Expected a UIAutomator hierarchy")
    return root


def blocker(xml):
    visible = {v for n in parse_ui(xml).iter("node") if bounds(n) for v in values(n)}
    categories = {
        "sign_in": ("sign in", "authentication is required", "verify it's you",
                    "需要进行身份验证", "需要验证身份", "您需要登录", "登录", "登入"),
        "unavailable": ("this app isn't available", "your device isn't compatible",
                        "您的设备与此版本不兼容", "此应用在您所在的国家", "找不到相应内容"),
        "user_confirmation": ("parental controls", "家长控制", "verify your age",
                              "purchase", "buy", "购买", "confirm your identity"),
    }
    for kind, phrases in categories.items():
        if any(phrase == value or (len(phrase) > 12 and phrase in value)
               for phrase in phrases for value in visible):
            return kind
    return None


@dataclass(frozen=True)
class Action:
    label: str
    label_bounds: tuple
    target_bounds: tuple
    layout: str

    @property
    def center(self):
        left, top, right, bottom = self.label_bounds
        return ((left + right) // 2, (top + bottom) // 2)

    def record(self):
        return asdict(self)


def find_install_action(xml, title, developer):
    """Bind an exact label to the first app header, including compact sheets.

    A label's center is used instead of the center of a composite clickable
    parent, which can also contain a separate 'install on more devices' button.
    """
    root = parse_ui(xml)
    parents = {child: parent for parent in root.iter() for child in parent}
    nodes = list(root.iter("node"))

    def visible(node):
        return (node.get("package") == PLAY and bounds(node)
                and node.get("enabled") != "false" and node.get("visible-to-user") != "false")

    def ancestors(node):
        while node in parents:
            node = parents[node]
            yield node

    def branch(node, container):
        while node in parents and parents[node] is not container:
            node = parents[node]
        return node if parents.get(node) is container else None

    titles = [n for n in nodes if visible(n) and normalized(title) in values(n)]
    developers = [n for n in nodes if visible(n) and normalized(developer) in values(n)]
    allowed = set(LABELS)
    actions = {}
    for label_node in nodes:
        if not visible(label_node) or not (values(label_node) & allowed):
            continue
        if any(n.get("enabled") == "false" or n.get("visible-to-user") == "false"
               for n in ancestors(label_node)):
            continue
        label = sorted(values(label_node) & allowed)[0]
        # Never reinterpret a compound description or a paid control as Install.
        if values(label_node) != {label}:
            continue
        target = label_node
        while target.get("clickable") != "true" and target in parents:
            target = parents[target]
        if not visible(target) or target.get("clickable") != "true":
            continue
        box, target_box = bounds(label_node), bounds(target)
        if not (target_box[0] <= box[0] < box[2] <= target_box[2]
                and target_box[1] <= box[1] < box[3] <= target_box[3]):
            continue
        point = ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)
        if any(n is not target and n.get("clickable") == "true" and bounds(n)
               and n.get("enabled") != "false" and n.get("visible-to-user") != "false"
               and bounds(n)[0] <= point[0] < bounds(n)[2]
               and bounds(n)[1] <= point[1] < bounds(n)[3]
               and n not in set(ancestors(target)) for n in nodes):
            continue
        for title_node in titles:
            for developer_node in developers:
                common = set(ancestors(developer_node)) & set(ancestors(target))
                for container in ancestors(title_node):
                    if container not in common or container.get("scrollable") != "true":
                        continue
                    header = branch(title_node, container)
                    if header is None or branch(developer_node, container) is not header:
                        continue
                    content = [c for c in container if any(visible(n) and values(n) for n in c.iter("node"))]
                    if not content or content[0] is not header:
                        continue
                    action_branch = branch(target, container)
                    if action_branch is header:
                        # Inline Install to the right of the app's identity.
                        identity_box = bounds(header)
                        t, d = bounds(title_node), bounds(developer_node)
                        if (identity_box and title_node is developer_node
                                and t[2] <= box[0]
                                and max(t[1], box[1]) < min(t[3], box[3])
                                and not any(n.get("scrollable") == "true" for n in header.iter("node"))):
                            actions[box] = Action(label, box, target_box, "compact_header")
                    elif action_branch is not None:
                        # Conventional full-width action after the header's stats.
                        children = list(container)
                        gap = children[children.index(header) + 1:children.index(action_branch)]
                        stat_values = [" ".join(v for n in c.iter("node") for v in values(n)) for c in gap]
                        markers = (("star", "星", "estrell", "étoile", "stern", "별"),
                                   ("rated", "content rating", "内容分级", "适合所有人", "everyone"),
                                   ("download", "下载", "descarga", "télécharg", "скачив"))
                        action_values = {v for n in action_branch.iter("node") for v in values(n)}
                        cbox = bounds(container)
                        if (len(gap) == 3 and all(any(m in v for m in ms) for v, ms in zip(stat_values, markers))
                                and not any(n.get("scrollable") == "true" for c in [*gap, action_branch] for n in c.iter("node"))
                                and action_values == {label} and cbox
                                and target_box[2] - target_box[0] >= (cbox[2] - cbox[0]) * .45
                                and max(bounds(title_node)[3], bounds(developer_node)[3]) <= box[1]):
                            actions[box] = Action(label, box, target_box, "full_details")
    if len(actions) > 1:
        raise UiError("Multiple Install controls are bound to the requested header")
    return next(iter(actions.values()), None)


def candidate_diagnostics(xml):
    return [{"values": sorted(values(n)), "bounds": bounds(n), "clickable": n.get("clickable") == "true"}
            for n in parse_ui(xml).iter("node")
            if n.get("package") == PLAY and bounds(n) and values(n) & set(LABELS)]


def listing_package(source):
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+", source):
        return source
    url = urlsplit(source)
    official = (url.scheme == "https" and url.hostname == "play.google.com"
                and re.fullmatch(r"/store/apps/details(?:/[^/]+)?/?", url.path))
    market = url.scheme == "market" and url.netloc == "details" and url.path in ("", "/")
    ids = [v for k, v in parse_qsl(url.query, keep_blank_values=True) if k == "id"]
    if (not (official or market) or url.username or url.password or url.fragment
            or len(ids) != 1
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+", ids[0])):
        raise UiError("Expected a Google Play listing or package ID; mirrors and APK URLs are not accepted")
    return ids[0]


def verify_activity(output, package):
    resumed = set(re.findall(
        r"^\s*(?:topResumedActivity=|ResumedActivity:\s*)(ActivityRecord\{[^}\n]+\})", output, re.M))
    if len(resumed) != 1:
        raise UiError("Resumed activity is unavailable")
    resumed = resumed.pop()
    records = {}
    current = None
    for line in output.splitlines():
        if re.match(r"\s*\* (?:Hist|Task)\b", line):
            current = None
            match = re.search(r"ActivityRecord\{[^}\n]+\}", line)
            if match:
                current = match.group()
                if current in records:
                    raise UiError("Duplicate activity identity")
                records[current] = []
        if current:
            records[current].append(line)

    def record(identity, state):
        match = re.fullmatch(r"ActivityRecord\{\S+ u(\d+) (\S+) t(\d+)\}", identity)
        text = "\n".join(records.get(identity, []))
        if (not match or not match[2].startswith(PLAY + "/")
                or not re.search(r"\bpackageName=" + re.escape(PLAY) + r"(?:\s|$)", text)
                or not re.search(r"^\s*state=" + state + r"\b(?=[^\n]*\bfinishing=false\b)", text, re.M)):
            raise UiError("Foreground Play context does not match its activity record")
        return match.groups(), text

    def bound(text):
        intents = re.findall(r"^\s*Intent \{([^}\n]*)\}\s*$", text, re.M)
        if len(intents) != 1:
            return False
        act = re.search(r"(?:^|\s)act=(\S+)", intents[0])
        data = re.search(r"(?:^|\s)dat=(\S+)", intents[0])
        if not act or act[1] != "android.intent.action.VIEW" or not data:
            return False
        try:
            return listing_package(data[1]) == package
        except UiError:
            # Play converts market:// links to this legacy URL inside its own
            # activity. This is an Intent check, never an HTTP download route.
            url = urlsplit(data[1])
            ids = [v for k, v in parse_qsl(url.query, keep_blank_values=True) if k == "id"]
            return (url.scheme in ("http", "https") and url.hostname == "market.android.com"
                    and url.path.rstrip("/") == "/details" and not url.username
                    and not url.password and not url.fragment and ids == [package])

    fields, text = record(resumed, "RESUMED")
    if bound(text):
        return
    links = re.findall(r"resultTo=(ActivityRecord\{[^}\n]+\})", text)
    if (fields[1] != SHEET or len(links) != 1
            or "act=com.google.android.finsky.launchInStoreBottomSheetDetailsPage " not in text):
        raise UiError("Foreground Play activity is not bound to the requested package")
    parent, parent_text = record(links[0], "PAUSED")
    if (parent[0] != fields[0] or parent[2] != fields[2]
            or parent[1] != DETAILS or not bound(parent_text)):
        raise UiError("Play sheet is not linked to the requested details task")
