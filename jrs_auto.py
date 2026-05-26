"""
JRS直播源自动解析上传
自动解析NBA/世界杯/欧洲杯/欧冠直播源并上传到Gitee
支持Playwright解析JS加密源
"""

import requests
import urllib3
import re
import json
import base64
import time
import os
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ========== 配置 ==========
GITEE_TOKEN = os.environ.get("GITEE_TOKEN", "1a6ab99cabeb34de3ad7ca64dcf38016")
GITEE_REPO = "tvbox-live"
KEY_LEAGUES = ["NBA", "世界杯", "欧洲杯", "欧冠"]
HEADERS = {'User-Agent': 'Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 Chrome/120.0.0.0 Mobile Safari/537.36'}

# CCTV频道筛选（只保留1、5、5+）
CCTV_KEEP = ['CCTV-1 ', 'CCTV-5 ', 'CCTV-5+',
             'CCTV1 ', 'CCTV5 ']


def parse_homepage():
    """解析首页，提取比赛列表"""
    print("1. 解析首页...")
    try:
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
    except Exception as e:
        print(f"   解析失败: {e}")
        return []


def extract_all_sources(play_url, use_playwright=True):
    """从直播页面提取所有源并解析m3u8"""
    parsed = urlparse(play_url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    try:
        resp = requests.get(play_url, headers=HEADERS, timeout=15, verify=False)
        resp.encoding = 'utf-8'
        html = resp.text
    except Exception:
        return []

    # 移除HTML注释，提取被注释掉的源
    html_clean = re.sub(r'<!--(.*?)-->', r'\1', html, flags=re.DOTALL)
    soup = BeautifulSoup(html_clean, "html.parser")
    sources = []
    for el in soup.find_all(attrs={"data-play": True}):
        src = el.get("data-play", "")
        name = el.get_text(strip=True)
        if src and src.strip() and src != '=&id2=':
            sources.append((name, src))

    # 用requests解析
    results = []
    failed_sources = []
    for source_name, src_path in sources:
        try:
            m3u8_result = parse_source(base, play_url, src_path)
            if m3u8_result:
                url, res_label = m3u8_result
                results.append((url, res_label, source_name))
            else:
                failed_sources.append((source_name, src_path))
        except Exception:
            failed_sources.append((source_name, src_path))

    print(f"   requests解析: {len(results)} 成功, {len(failed_sources)} 失败")

    # 用Playwright一次性捕获所有m3u8源（包括JS动态加载的）
    if use_playwright and (failed_sources or len(sources) < 9):
        pw_results = playwright_full_capture(base, play_url, failed_sources)
        # 合并去重（按m3u8路径去重）
        seen_paths = set(urlparse(u).path for u, _, _ in results)
        for url, res_label, source_name in pw_results:
            path = urlparse(url).path
            if path not in seen_paths:
                seen_paths.add(path)
                results.append((url, res_label, source_name))

    return results


def playwright_full_capture(base, play_url, failed_sources):
    """用Playwright一次性捕获所有m3u8源（失败源+JS动态源）"""
    results = []
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("   Playwright未安装，跳过")
        return results

    total = len(failed_sources)
    print(f"   Playwright捕获 {total} 个失败源 + JS动态源...")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage']
            )
            context = browser.new_context(
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            )
            page = context.new_page()

            all_m3u8 = []

            def handle_request(request):
                url = request.url
                if '.m3u8' in url and 'msss.html' not in url:
                    all_m3u8.append(url)
                    print(f"     [请求] m3u8: {url[:80]}")

            def handle_response(response):
                url = response.url
                if '.m3u8' in url and 'msss.html' not in url and url not in all_m3u8:
                    all_m3u8.append(url)
                    print(f"     [响应] m3u8: {url[:80]}")

            # 监听请求和响应（双保险）
            page.on('request', handle_request)
            page.on('response', handle_response)
            try:
                context.on('request', handle_request)
            except Exception:
                print("     context.on不支持，使用page.on")

            # 第一步：访问直播页面，点击所有播放按钮
            try:
                print(f"     访问直播页: {play_url[:60]}")
                page.goto(play_url, timeout=30000, wait_until='domcontentloaded')
                page.wait_for_timeout(5000)
                btns = page.query_selector_all('[data-play]')
                print(f"     找到 {len(btns)} 个播放按钮")
                for i, btn in enumerate(btns):
                    try:
                        btn.click(timeout=5000)
                        page.wait_for_timeout(3000)
                    except Exception as e:
                        print(f"     按钮{i+1}点击: {type(e).__name__}")
            except Exception as e:
                print(f"     直播页面: {type(e).__name__}: {str(e)[:80]}")

            # 第二步：对requests失败的源，逐个导航到其URL
            for source_name, src_path in failed_sources:
                full_url = urljoin(base, src_path) if src_path.startswith('/') else src_path
                print(f"     访问失败源: {full_url[:60]}")
                try:
                    page.goto(full_url, timeout=30000, wait_until='domcontentloaded')
                    page.wait_for_timeout(5000)
                    # 从页面内容中提取m3u8（备用方案）
                    try:
                        content = page.content()
                        m3u8_matches = re.findall(r'(https?://[^"\'<>\s\\]+\.m3u8[^"\'<>\s\\]*)', content)
                        for m in m3u8_matches:
                            if m not in all_m3u8 and 'msss.html' not in m:
                                all_m3u8.append(m)
                                print(f"     [页面] m3u8: {m[:80]}")
                    except Exception:
                        pass
                    # 从iframe内容提取
                    for frame in page.frames:
                        try:
                            frame_content = frame.content()
                            frame_m3u8 = re.findall(r'(https?://[^"\'<>\s\\]+\.m3u8[^"\'<>\s\\]*)', frame_content)
                            for m in frame_m3u8:
                                if m not in all_m3u8 and 'msss.html' not in m:
                                    all_m3u8.append(m)
                                    print(f"     [iframe] m3u8: {m[:80]}")
                        except Exception:
                            pass
                except Exception as e:
                    print(f"     访问失败: {type(e).__name__}: {str(e)[:60]}")

            page.remove_listener('request', handle_request)
            page.remove_listener('response', handle_response)
            browser.close()

            unique_m3u8 = list(set(all_m3u8))
            print(f"     Playwright共捕获 {len(unique_m3u8)} 个m3u8")
            for url in unique_m3u8:
                results.append((url, "", "Playwright"))

    except Exception as e:
        print(f"   Playwright捕获失败: {type(e).__name__}: {str(e)[:100]}")
    return results


def parse_source(base, play_url, src_path):
    """解析单个源路径"""
    full_url = urljoin(base, src_path) if src_path.startswith('/') else src_path
    try:
        r = requests.get(full_url, headers=HEADERS, timeout=10, verify=False)
        content = r.content.decode('utf-8', errors='ignore')
    except Exception as e:
        print(f"     请求失败 {src_path[:30]}: {type(e).__name__}")
        return None

    # sm.html
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
                result = resolve_m3u8("https://hdl6.szsummer.cn" + m3u8_path)
                if result:
                    print(f"     sm.html解析成功: id={id_match.group(1)}")
                    return result
                else:
                    print(f"     sm.html m3u8解析失败: id={id_match.group(1)} path={m3u8_path[:40]}")
            else:
                print(f"     sm.html未找到m3u8路径: id={id_match.group(1)} detail_len={len(detail)}")
        except Exception as e:
            print(f"     sm.html请求失败: id={id_match.group(1)} {type(e).__name__}")

    # JS iframe
    js_iframe = re.search(r"src=['\"](\.?/?play/\w+\.php\?[^'\"]+)['\"]", content)
    if not js_iframe:
        js_iframe = re.search(r"src=['\"](\.?/?\w+\.php\?[^'\"]+)['\"]", content)
    if js_iframe:
        iframe_url = urljoin(full_url, js_iframe.group(1))
        try:
            r2 = requests.get(iframe_url, headers=HEADERS, timeout=10, verify=False)
            content2 = r2.content.decode('utf-8', errors='ignore')
            m = re.search(r'(https?://[^"\'<>\s]+\.m3u8[^"\'<>\s]*)', content2)
            if m:
                url = m.group(1)
                return (url, "")
            js_iframe2 = re.search(r"src=['\"]([^'\"]+)['\"]", content2)
            if js_iframe2:
                iframe2_url = urljoin(iframe_url, js_iframe2.group(1))
                try:
                    r3 = requests.get(iframe2_url, headers=HEADERS, timeout=10, verify=False)
                    content3 = r3.content.decode('utf-8', errors='ignore')
                    m2 = re.search(r'(https?://[^"\'<>\s]+\.m3u8[^"\'<>\s]*)', content3)
                    if m2:
                        url = m2.group(1)
                        return (url, "")
                except Exception:
                    pass
        except Exception:
            pass

    # HTML iframe
    soup = BeautifulSoup(content, 'html.parser')
    iframe = soup.find('iframe')
    if iframe and iframe.get('src'):
        iframe_url = urljoin(full_url, iframe['src'])
        try:
            r2 = requests.get(iframe_url, headers=HEADERS, timeout=10, verify=False)
            content2 = r2.content.decode('utf-8', errors='ignore')
            m = re.search(r'(https?://[^"\'<>\s]+\.m3u8[^"\'<>\s]*)', content2)
            if m:
                url = m.group(1)
                return (url, "")
        except Exception:
            pass

    # 直接m3u8
    m = re.search(r'(https?://[^"\'<>\s]+\.m3u8[^"\'<>\s]*)', content)
    if m:
        url = m.group(1)
        return (url, "")
    return None


def resolve_m3u8(master_url):
    """解析主m3u8"""
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
                            return (url_line, res_label)
    except Exception:
        pass
    return (master_url, "")


def deep_parse(matches):
    """深度解析所有比赛"""
    print("\n2. 深度解析直播源...")
    results = []
    for idx, match in enumerate(matches):
        name = f"{match['home']} vs {match['away']}"
        print(f"   [{idx+1}/{len(matches)}] {match['league']}: {name}")

        m3u8_urls = []
        seen_paths = set()
        links = match.get("links", [])

        # 第一步：所有链接先用requests解析
        all_link_results = []
        for li, link in enumerate(links):
            try:
                src = extract_all_sources(link, use_playwright=False)
                all_link_results.append((li, link, src))
            except Exception:
                all_link_results.append((li, link, []))

        # 第二步：找到源最多的链接，对其启用Playwright捕获额外源
        best_li, best_link, best_src = max(all_link_results, key=lambda x: len(x[2]))
        if best_src or True:  # 始终尝试Playwright
            try:
                pw_src = extract_all_sources(best_link, use_playwright=True)
                # 合并Playwright结果
                for url, res_label, source_name in pw_src:
                    path_key = urlparse(url).path
                    if path_key not in seen_paths:
                        seen_paths.add(path_key)
                        m3u8_urls.append((url, res_label, source_name))
            except Exception:
                pass

        # 合并所有链接的requests结果
        for li, link, src in all_link_results:
            for url, res_label, source_name in src:
                path_key = urlparse(url).path
                if path_key not in seen_paths:
                    seen_paths.add(path_key)
                    m3u8_urls.append((url, res_label, source_name))

        if m3u8_urls:
            results.append({
                'league': match['league'], 'home': match['home'],
                'away': match['away'], 'm3u8_urls': m3u8_urls
            })
            print(f"      -> {len(m3u8_urls)} 个源")
        else:
            print(f"      -> 解析失败")
    return results


def parse_m3u_playlist(url, filter_keywords=None, filter_cctv=False):
    """解析m3u播放列表"""
    channels = []
    try:
        r = requests.get(url, headers=HEADERS, timeout=15, verify=False)
        lines = r.text.strip().split('\n')
        current_name = ""
        for line in lines:
            line = line.strip()
            if line.startswith('#EXTINF'):
                name = line.split(',')[-1].strip()
                current_name = name
            elif line.startswith('http') and current_name:
                if filter_cctv:
                    if not any(c in current_name for c in CCTV_KEEP):
                        current_name = ""
                        continue
                if filter_keywords and not any(kw in current_name for kw in filter_keywords):
                    current_name = ""
                    continue
                # 跳过非视频流URL
                if 'youtube.com' in line:
                    current_name = ""
                    continue
                stream_url = line
                # 不强制HTTP转HTTPS（IP地址源HTTPS会SSL错误）
                # 清理频道名中的特殊字符（TVBox兼容）
                current_name = re.sub(r'\[.*?\]', '', current_name).strip()
                current_name = re.sub(r'\(.*?\)', '', current_name).strip()
                current_name = current_name.replace('|', '').replace('  ', ' ').strip()
                if not current_name:
                    continue
                channels.append(f"{current_name},{stream_url}")
                current_name = ""
    except Exception as e:
        print(f"   解析失败: {e}")
    return channels


def parse_tvbox_txt(url, filter_names=None):
    """解析TVBox格式的txt直播源（分类名,#genre# + 频道名,URL）"""
    channels = []
    try:
        r = requests.get(url, headers=HEADERS, timeout=15, verify=False)
        # 尝试多种编码
        for enc in ['utf-8', 'gbk', 'gb2312', 'latin-1']:
            try:
                text = r.content.decode(enc)
                if '频道' in text or 'CCTV' in text or '#genre#' in text:
                    break
            except Exception:
                continue
        else:
            text = r.content.decode('utf-8', errors='ignore')

        lines = text.strip().split('\n')
        for line in lines:
            line = line.strip()
            if not line or '#genre#' in line or line.startswith('#'):
                continue
            # 格式: 频道名,URL  或  频道名,URL1#URL2#URL3
            parts = line.split(',', 1)
            if len(parts) == 2 and parts[1].strip().startswith('http'):
                name = parts[0].strip()
                stream_url = parts[1].strip()
                # 筛选指定频道名
                if filter_names and not any(fn in name for fn in filter_names):
                    continue
                # 跳过非视频流URL（YouTube等）
                if 'youtube.com' in stream_url:
                    continue
                # 不强制HTTP转HTTPS
                channels.append(f"{name},{stream_url}")
    except Exception as e:
        print(f"   解析失败: {e}")
    return channels


def parse_iptv_sources():
    """解析IPTV直播源"""
    print("\n3. 解析IPTV直播源...")
    all_channels = {}

    # 解析TVBox接口中的直播源（巧技等）
    print("   解析TVBox接口直播源...")
    tvbox_configs = [
        'http://cdn.qiaoji8.com/tvbox.json',
    ]
    for config_url in tvbox_configs:
        try:
            r = requests.get(config_url, headers=HEADERS, timeout=10, verify=False)
            d = r.json()
            lives = d.get('lives', [])
            for live in lives:
                live_url = live.get('url', '')
                live_name = live.get('name', '')
                if live_url and live_url.startswith('http'):
                    try:
                        ch = parse_tvbox_txt(live_url)
                        if ch:
                            all_channels[f"直播源-{live_name}"] = ch
                            print(f"   从{live_name}获取{len(ch)}个频道")
                    except Exception:
                        pass
        except Exception as e:
            print(f"   解析{config_url}失败: {type(e).__name__}")

    total = sum(len(v) for v in all_channels.values())
    print(f"   共解析 {total} 个IPTV频道")
    return all_channels


def gen_live_txt(matches, iptv_channels):
    """生成live.txt内容"""
    print("\n4. 生成直播源文件...")
    groups = {}

    # JRS直播源
    for m in matches:
        league = m['league']
        name = f"{m['home']} vs {m['away']}"
        if league not in groups:
            groups[league] = []
        for url, res_label, source_name in m['m3u8_urls']:
            groups[league].append(f"{name},{url}")

    # IPTV频道
    for category, channels in iptv_channels.items():
        groups[category] = channels

    lines = []
    for league, channels in groups.items():
        lines.append(f"{league},#genre#")
        for ch in channels:
            lines.append(ch)
        lines.append("")

    content = '\n'.join(lines)
    total = sum(len(v) for v in groups.values())
    print(f"   生成 {len(groups)} 个分类，{total} 个频道")
    return content


def upload_to_gitee(live_content):
    """上传到Gitee"""
    print("\n5. 上传到Gitee...")
    api = "https://gitee.com/api/v5"
    token = GITEE_TOKEN

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

    r = requests.get(f"{api}/repos/{owner}/{repo_name}", params={"access_token": token}, timeout=10, verify=False)
    if r.status_code != 200:
        create_data = {
            "access_token": token, "name": repo_name,
            "description": "JRS直播源", "private": False, "auto_init": True
        }
        r2 = requests.post(f"{api}/user/repos", json=create_data, timeout=15, verify=False)
        if r2.status_code not in (201, 200):
            print(f"   创建仓库失败: {r2.text[:200]}")
            return None
        print(f"   创建仓库: {repo_name}")
        time.sleep(2)
    else:
        repo_data = r.json()
        if repo_data.get("private"):
            requests.patch(f"{api}/repos/{owner}/{repo_name}",
                          json={"access_token": token, "private": False}, timeout=10, verify=False)

    live_url = f"https://gitee.com/{owner}/{repo_name}/raw/master/live.txt"
    config = {
        "lives": [{
            "name": "JRS直播",
            "type": 0,
            "url": live_url,
            "playerType": 2
        }]
    }
    config_content = json.dumps(config, ensure_ascii=False, indent=2)

    for filename, content in [("live.txt", live_content), ("config.json", config_content)]:
        r = requests.get(f"{api}/repos/{owner}/{repo_name}/contents/{filename}",
                        params={"access_token": token}, timeout=10, verify=False)
        b64_content = base64.b64encode(content.encode('utf-8')).decode('utf-8')

        if r.status_code == 200:
            data = r.json()
            sha = data[0].get("sha", "") if isinstance(data, list) and data else data.get("sha", "")
            r2 = requests.put(f"{api}/repos/{owner}/{repo_name}/contents/{filename}",
                json={"access_token": token, "content": b64_content, "sha": sha,
                      "message": f"更新 {filename} {time.strftime('%H:%M')}"}, timeout=15, verify=False)
        else:
            r2 = requests.post(f"{api}/repos/{owner}/{repo_name}/contents/{filename}",
                json={"access_token": token, "content": b64_content,
                      "message": f"添加 {filename}"}, timeout=15, verify=False)

        if r2.status_code in (200, 201):
            print(f"   {filename} 上传成功")
        else:
            print(f"   {filename} 上传失败: {r2.status_code}")

    config_url = f"https://gitee.com/{owner}/{repo_name}/raw/master/config.json"
    live_txt_url = f"https://gitee.com/{owner}/{repo_name}/raw/master/live.txt"

    # 验证Gitee返回的是纯文本而非HTML页面
    try:
        check = requests.get(live_txt_url, headers=HEADERS, timeout=10, verify=False)
        if '<html' in check.text.lower() or '#genre#' not in check.text:
            print(f"   警告: live.txt可能未正确返回纯文本!")
            print(f"   前200字符: {check.text[:200]}")
        else:
            print(f"   验证: live.txt正常 ({len(check.text.splitlines())} 行)")
    except Exception as e:
        print(f"   验证失败: {e}")

    return config_url, live_txt_url


def main():
    print("=" * 50)
    print("JRS直播源自动解析上传")
    print(f"重点赛事: {', '.join(KEY_LEAGUES)}")
    print("=" * 50)

    matches = parse_homepage()
    results = deep_parse(matches) if matches else []
    iptv_channels = parse_iptv_sources()
    live_content = gen_live_txt(results, iptv_channels)
    urls = upload_to_gitee(live_content)

    if urls:
        config_url, live_url = urls
        print("\n" + "=" * 50)
        print("上传成功!")
        print(f"\nTVBox配置地址: {config_url}")
        print(f"直播源地址: {live_url}")
        print("=" * 50)


if __name__ == "__main__":
    main()
