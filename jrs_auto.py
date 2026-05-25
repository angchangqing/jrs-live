"""
JRS直播源自动解析上传 - 手机版
用法: python jrs_auto.py
自动解析NBA/世界杯/欧洲杯/欧冠直播源并上传到Gitee
"""

import requests
import urllib3
import re
import json
import base64
import time
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ========== 配置 ==========
import os
GITEE_TOKEN = os.environ.get("GITEE_TOKEN", "1a6ab99cabeb34de3ad7ca64dcf38016")
GITEE_REPO = "tvbox-live"
KEY_LEAGUES = ["NBA", "世界杯", "欧洲杯", "欧冠"]
HEADERS = {'User-Agent': 'Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 Chrome/120.0.0.0 Mobile Safari/537.36'}


def parse_homepage():
    """解析首页，提取比赛列表"""
    print("1. 解析首页...")
    resp = requests.get('https://m.jrs03.com', headers=HEADERS, timeout=15, verify=False)
    resp.encoding = 'utf-8'
    soup = BeautifulSoup(resp.text, 'html.parser')
    blocks = soup.find_all(attrs={'data-lid': True})

    matches = []
    for b in blocks:
        league_el = b.find('li', class_='lab_events')
        league = league_el.find('span', class_='name').get_text(strip=True) if league_el else ''
        home_el = b.find('li', class_='lab_team_home')
        away_el = b.find('li', class_='lab_team_away')
        home = home_el.get_text(strip=True) if home_el else ''
        away = away_el.get_text(strip=True) if away_el else ''
        links = [a.get('data-play', '') for a in b.find_all('a', attrs={'data-play': True})]

        is_key = any(kw in league.upper() for kw in KEY_LEAGUES)
        matches.append({
            'league': league, 'home': home, 'away': away,
            'links': links, 'is_key': is_key
        })

    key_matches = [m for m in matches if m['is_key']]
    print(f"   共{len(matches)}场比赛，重点赛事{len(key_matches)}场")
    for m in key_matches:
        print(f"   - {m['league']}: {m['home']} vs {m['away']}")
    return key_matches


def extract_all_sources(play_url):
    """从直播页面提取所有源并解析m3u8"""
    parsed = urlparse(play_url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    try:
        resp = requests.get(play_url, headers=HEADERS, timeout=15, verify=False)
        resp.encoding = 'utf-8'
        html = resp.text
    except Exception:
        return []

    soup = BeautifulSoup(html, "html.parser")
    sources = []
    for el in soup.find_all(attrs={"data-play": True}):
        src = el.get("data-play", "")
        name = el.get_text(strip=True)
        if src:
            sources.append((name, src))

    results = []
    for source_name, src_path in sources:
        try:
            m3u8_result = parse_source(base, play_url, src_path)
            if m3u8_result:
                url, res_label = m3u8_result
                results.append((url, res_label, source_name))
        except Exception:
            continue
    return results


def parse_source(base, play_url, src_path):
    """解析单个源路径"""
    full_url = urljoin(base, src_path) if src_path.startswith('/') else src_path

    try:
        r = requests.get(full_url, headers=HEADERS, timeout=10, verify=False)
        content = r.content.decode('utf-8', errors='ignore')
    except Exception:
        return None

    # sm.html -> id -> detail -> m3u8
    id_match = re.search(r'[?&]id=(\d+)', src_path)
    if id_match and 'sm.html' in src_path:
        detail_url = base + '/play/' + id_match.group(1) + '.html'
        try:
            r2 = requests.get(detail_url, headers=HEADERS, timeout=10, verify=False)
            detail = r2.content.decode('utf-8', errors='ignore')
            m3u8_path = None
            m = re.search(r'msss\.html\?id=([^"\'<>\s]+)', detail)
            if m: m3u8_path = m.group(1)
            if not m3u8_path:
                m = re.search(r'(/live/\d+\.m3u8[^"\'<>\s]*)', detail)
                if m: m3u8_path = m.group(1)
            if m3u8_path:
                return resolve_m3u8("https://hdl6.szsummer.cn" + m3u8_path)
        except Exception:
            pass

    # JS中的iframe (gm.php/kbs.html -> kbmm.php/mgw.php)
    js_iframe_match = re.search(r"src=['\"](\.?/?play/\w+\.php\?[^'\"]+)['\"]", content)
    if not js_iframe_match:
        js_iframe_match = re.search(r"src=['\"](\.?/?\w+\.php\?[^'\"]+)['\"]", content)
    if js_iframe_match:
        iframe_path = js_iframe_match.group(1)
        iframe_url = urljoin(full_url, iframe_path)
        try:
            r2 = requests.get(iframe_url, headers=HEADERS, timeout=10, verify=False)
            content2 = r2.content.decode('utf-8', errors='ignore')
            m = re.search(r'(https?://[^"\'<>\s]+\.m3u8[^"\'<>\s]*)', content2)
            if m:
                url = m.group(1)
                if url.startswith('http://'): url = 'https://' + url[7:]
                return (url, "")
            # 嵌套iframe
            js_iframe2 = re.search(r"src=['\"]([^'\"]+)['\"]", content2)
            if js_iframe2:
                iframe2_url = urljoin(iframe_url, js_iframe2.group(1))
                try:
                    r3 = requests.get(iframe2_url, headers=HEADERS, timeout=10, verify=False)
                    content3 = r3.content.decode('utf-8', errors='ignore')
                    m2 = re.search(r'(https?://[^"\'<>\s]+\.m3u8[^"\'<>\s]*)', content3)
                    if m2:
                        url = m2.group(1)
                        if url.startswith('http://'): url = 'https://' + url[7:]
                        return (url, "")
                except Exception:
                    pass
        except Exception:
            pass

    # HTML iframe
    soup = BeautifulSoup(content, 'html.parser')
    iframe = soup.find('iframe')
    if iframe:
        iframe_src = iframe.get('src', '')
        if iframe_src:
            iframe_url = urljoin(full_url, iframe_src)
            try:
                r2 = requests.get(iframe_url, headers=HEADERS, timeout=10, verify=False)
                content2 = r2.content.decode('utf-8', errors='ignore')
                m = re.search(r'(https?://[^"\'<>\s]+\.m3u8[^"\'<>\s]*)', content2)
                if m:
                    url = m.group(1)
                    if url.startswith('http://'): url = 'https://' + url[7:]
                    return (url, "")
            except Exception:
                pass

    # 直接m3u8
    m = re.search(r'(https?://[^"\'<>\s]+\.m3u8[^"\'<>\s]*)', content)
    if m:
        url = m.group(1)
        if url.startswith('http://'): url = 'https://' + url[7:]
        return (url, "")
    return None


def resolve_m3u8(master_url):
    """解析主m3u8，返回子流"""
    try:
        r = requests.get(master_url, headers=HEADERS, timeout=10, verify=False)
        if r.status_code == 200 and '#EXT-X-STREAM-INF' in r.text:
            lines_list = r.text.split('\n')
            for i, line in enumerate(lines_list):
                line = line.strip()
                if line.startswith('#EXT-X-STREAM-INF'):
                    res_match = re.search(r'RESOLUTION=(\d+)x(\d+)', line)
                    res_label = ""
                    if res_match:
                        h = int(res_match.group(2))
                        if h >= 1080: res_label = "1080P"
                        elif h >= 720: res_label = "720P"
                        elif h >= 480: res_label = "480P"
                        else: res_label = f"{h}P"
                    if i + 1 < len(lines_list):
                        url_line = lines_list[i + 1].strip()
                        if url_line.startswith('http'):
                            if url_line.startswith('http://'): url_line = 'https://' + url_line[7:]
                            return (url_line, res_label)
    except Exception:
        pass
    return (master_url, "")


def deep_parse(matches):
    """深度解析所有比赛"""
    print("\n2. 深度解析直播源...")
    results = []
    total = len(matches)

    for idx, match in enumerate(matches):
        name = f"{match['home']} vs {match['away']}"
        print(f"   [{idx+1}/{total}] {match['league']}: {name}")

        m3u8_urls = []
        seen_paths = set()
        for link in match.get("links", []):
            try:
                all_src = extract_all_sources(link)
                for url, res_label, source_name in all_src:
                    path_key = urlparse(url).path
                    if path_key not in seen_paths:
                        seen_paths.add(path_key)
                        m3u8_urls.append((url, res_label, source_name))
            except Exception:
                continue

        if m3u8_urls:
            results.append({
                'league': match['league'],
                'home': match['home'],
                'away': match['away'],
                'm3u8_urls': m3u8_urls
            })
            print(f"      -> {len(m3u8_urls)} 个源")
        else:
            print(f"      -> 解析失败")

    return results


def gen_live_txt(matches):
    """生成live.txt内容"""
    print("\n3. 生成直播源文件...")
    groups = {}
    for m in matches:
        league = m['league']
        name = f"{m['home']} vs {m['away']}"
        if league not in groups:
            groups[league] = []
        for url, res_label, source_name in m['m3u8_urls']:
            groups[league].append(f"{name},{url}")

    lines = []
    for league, channels in groups.items():
        lines.append(f"{league},#genre#")
        for ch in channels:
            lines.append(ch)
        lines.append("")

    content = '\n'.join(lines)
    print(f"   生成 {len(groups)} 个分类，{sum(len(v) for v in groups.values())} 个频道")
    return content


def upload_to_gitee(live_content):
    """上传到Gitee"""
    print("\n4. 上传到Gitee...")
    api = "https://gitee.com/api/v5"
    token = GITEE_TOKEN

    # 获取用户信息
    try:
        r = requests.get(f"{api}/user", params={"access_token": token}, timeout=10, verify=False)
        if r.status_code != 200:
            print("   错误: Token无效")
            return None
        data = r.json()
        owner = data.get("login") or data.get("path")
        print(f"   用户: {owner}")
    except Exception as e:
        print(f"   错误: {e}")
        return None

    repo_name = GITEE_REPO

    # 确保仓库存在
    r = requests.get(f"{api}/repos/{owner}/{repo_name}", params={"access_token": token}, timeout=10, verify=False)
    if r.status_code != 200:
        # 创建仓库
        create_data = {
            "access_token": token,
            "name": repo_name,
            "description": "JRS直播源",
            "private": False,
            "auto_init": True
        }
        r2 = requests.post(f"{api}/user/repos", json=create_data, timeout=15, verify=False)
        if r2.status_code not in (201, 200):
            print(f"   创建仓库失败: {r2.text[:200]}")
            return None
        print(f"   创建仓库: {repo_name}")
        time.sleep(2)
    else:
        # 确保公开
        repo_data = r.json()
        if repo_data.get("private"):
            requests.patch(f"{api}/repos/{owner}/{repo_name}",
                          json={"access_token": token, "private": False}, timeout=10, verify=False)

    # 生成config.json
    live_url = f"https://gitee.com/{owner}/{repo_name}/raw/master/live.txt"
    ts = int(time.time())
    config = {
        "lives": [{
            "name": "JRS直播",
            "type": 0,
            "url": f"{live_url}?t={ts}",
            "playerType": 2
        }]
    }
    config_content = json.dumps(config, ensure_ascii=False, indent=2)

    # 上传文件
    for filename, content in [("live.txt", live_content), ("config.json", config_content)]:
        # 检查文件是否存在
        r = requests.get(f"{api}/repos/{owner}/{repo_name}/contents/{filename}",
                        params={"access_token": token}, timeout=10, verify=False)

        b64_content = base64.b64encode(content.encode('utf-8')).decode('utf-8')

        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list):
                sha = data[0].get("sha", "") if data else ""
            else:
                sha = data.get("sha", "")
            # 更新
            r2 = requests.put(f"{api}/repos/{owner}/{repo_name}/contents/{filename}",
                json={"access_token": token, "content": b64_content, "sha": sha,
                      "message": f"更新 {filename} {time.strftime('%H:%M')}"}, timeout=15, verify=False)
        else:
            # 创建
            r2 = requests.post(f"{api}/repos/{owner}/{repo_name}/contents/{filename}",
                json={"access_token": token, "content": b64_content,
                      "message": f"添加 {filename}"}, timeout=15, verify=False)

        if r2.status_code in (200, 201):
            print(f"   {filename} 上传成功")
        else:
            print(f"   {filename} 上传失败: {r2.status_code}")

    config_url = f"https://gitee.com/{owner}/{repo_name}/raw/master/config.json"
    live_txt_url = f"https://gitee.com/{owner}/{repo_name}/raw/master/live.txt"
    return config_url, live_txt_url


def main():
    print("=" * 50)
    print("JRS直播源自动解析上传")
    print(f"重点赛事: {', '.join(KEY_LEAGUES)}")
    print("=" * 50)

    # 1. 解析首页
    matches = parse_homepage()
    if not matches:
        print("\n当前无重点赛事，程序结束")
        return

    # 2. 深度解析
    results = deep_parse(matches)
    if not results:
        print("\n解析失败，程序结束")
        return

    # 3. 生成live.txt
    live_content = gen_live_txt(results)

    # 4. 上传
    urls = upload_to_gitee(live_content)
    if urls:
        config_url, live_url = urls
        print("\n" + "=" * 50)
        print("上传成功!")
        print(f"\nTVBox配置地址: {config_url}")
        print(f"直播源地址: {live_url}")
        print("\nTVBox使用方法:")
        print("  方式1: 设置 → 配置地址 → 粘贴config.json地址")
        print("  方式2: 设置 → 直播地址 → 粘贴live.txt地址")
        print("=" * 50)


if __name__ == "__main__":
    main()
