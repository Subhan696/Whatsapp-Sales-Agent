import sys

file_path = r'd:\projects\MCP\Whatsapp AI\app\admin\router.py'
with open(file_path, 'r', encoding='utf-8') as f:
    for i, line in enumerate(f, 1):
        if 'auth-overlay' in line or 'class="topbar"' in line or 'class="tabs"' in line:
            print(f'Line {i}: {line.strip()}')
