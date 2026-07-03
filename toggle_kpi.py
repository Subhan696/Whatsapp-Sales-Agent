import sys

file_path = r'd:\projects\MCP\Whatsapp AI\app\admin\router.py'
with open(file_path, 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Insert the toggle button right before <div class="kpi-grid" id="kpi-grid">
kpi_html = '<div class="kpi-grid" id="kpi-grid">'
toggle_html = '''<div style="padding: 12px 24px 0; display: flex; justify-content: flex-end;">
  <button onclick="toggleKPIs()" id="kpi-toggle-btn" style="background: rgba(0,0,0,0.05); border: none; padding: 4px 10px; border-radius: 12px; color: #6b7280; font-size: 11px; font-weight: 600; cursor: pointer; display: flex; align-items: center; gap: 4px; transition: background 0.2s;">
    &#128065; Hide Summary
  </button>
</div>
<div class="kpi-grid" id="kpi-grid">'''

if kpi_html in content and 'kpi-toggle-btn' not in content:
    content = content.replace(kpi_html, toggle_html)
    print("Toggle button HTML injected.")
elif 'kpi-toggle-btn' in content:
    print("Toggle button already exists.")

# 2. Insert the JS logic right before the closing </body> tag
js_logic = '''
<script>
function toggleKPIs() {
  const grid = document.getElementById('kpi-grid');
  const btn = document.getElementById('kpi-toggle-btn');
  // If we're on mobile, grid uses grid-template-columns: repeat(2, 1fr) !important, 
  // so we should just toggle a hidden class or use display: none
  if (grid.style.display === 'none') {
    grid.style.display = ''; // revert to stylesheet display
    btn.innerHTML = '&#128065; Hide Summary';
    localStorage.setItem('hideKPIs', 'false');
  } else {
    grid.style.display = 'none';
    btn.innerHTML = '&#128064; Show Summary';
    localStorage.setItem('hideKPIs', 'true');
  }
}

// Apply on load
document.addEventListener("DOMContentLoaded", () => {
  if (localStorage.getItem('hideKPIs') === 'true') {
    const grid = document.getElementById('kpi-grid');
    const btn = document.getElementById('kpi-toggle-btn');
    if (grid && btn) {
      grid.style.display = 'none';
      btn.innerHTML = '&#128064; Show Summary';
    }
  }
});
</script>
</body>'''

if 'function toggleKPIs()' not in content:
    content = content.replace('</body>', js_logic)
    print("Toggle JS logic injected.")

with open(file_path, 'w', encoding='utf-8') as f:
    f.write(content)

print("KPI Toggle script completed.")
