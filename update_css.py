import sys
import re

file_path = r'd:\projects\MCP\Whatsapp AI\app\admin\router.py'
with open(file_path, 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Update HTML for topbar to include hamburger button
old_topbar = '''<div class="topbar">
  <h1><div class="wa-dot">&#128241;</div> Business <span>CRM</span></h1>'''
new_topbar = '''<div class="topbar">
  <div class="topbar-left">
    <button class="mobile-menu-btn" onclick="document.getElementById('tabs').classList.toggle('open')">&#9776;</button>
    <h1><div class="wa-dot">&#128241;</div> Business <span>CRM</span></h1>
  </div>'''

if old_topbar in content:
    content = content.replace(old_topbar, new_topbar)
    print("Topbar HTML updated with hamburger button.")
elif 'class="mobile-menu-btn"' in content:
    print("Hamburger button already present.")

# 2. Update HTML for tabs to close the menu when clicked
# The tabs look like: <button class="tab" onclick="showTab('orders',this)">
# We can just do a regex replace to append `; document.getElementById('tabs').classList.remove('open')` to the onclick
pattern_onclick = r'onclick="(showTab\([^"]+\))"'
content = re.sub(pattern_onclick, r'onclick="\1; document.getElementById(\'tabs\').classList.remove(\'open\')"', content)

# 3. Modify the CSS for the hamburger and dropdown menu
mobile_css = '''
  /* Modern Mobile App Principles */
  .mobile-menu-btn { display: none; }
  .topbar-left { display: flex; align-items: center; }

  @media (max-width: 768px) {
    .mobile-menu-btn {
      display: block !important;
      background: none;
      border: none;
      color: #fff;
      font-size: 20px;
      cursor: pointer;
      padding: 0 12px 0 0;
      line-height: 1;
    }
    
    /* Tabs as a Dropdown Menu */
    .tabs {
      position: absolute;
      top: 60px;
      left: 0;
      right: 0;
      background: #fff;
      flex-direction: column;
      padding: 12px;
      box-shadow: 0 10px 24px rgba(0,0,0,0.15);
      z-index: 1000;
      display: none !important;
      overflow: visible;
      border-radius: 0 0 16px 16px;
      gap: 4px;
    }
    .tabs.open {
      display: flex !important;
      animation: slideDown 0.2s ease;
    }
    @keyframes slideDown {
      from { transform: translateY(-10px); opacity: 0; }
      to { transform: translateY(0); opacity: 1; }
    }
    .tab {
      flex-direction: row;
      justify-content: flex-start;
      font-size: 14px;
      padding: 14px 16px;
      border-radius: 12px;
      color: #374151;
      width: 100%;
      text-align: left;
      border-bottom: none !important;
    }
    .tab.active {
      background: #f0fdf4;
      color: #075E54;
      box-shadow: none;
    }
    .tab.active::after { display: none; }
    
    body { padding-bottom: 20px; } /* Reset from bottom nav */
    
    /* KPI Cards - Wrap and reduce size */
    .kpi-grid { 
      grid-template-columns: repeat(2, 1fr) !important; 
      gap: 12px; 
      padding: 16px 16px 0; 
    }
    .kpi-card { 
      padding: 14px; 
      border-radius: 12px; 
      margin-bottom: 0; 
    }
    .kpi-card .value { font-size: 20px; }
    .kpi-card .label { font-size: 9px; margin-bottom: 4px; }
    .kpi-card .kpi-icon { font-size: 18px; margin-bottom: 8px; }
    .kpi-card .sub { font-size: 10px; }

    /* Topbar - Hide non-essential elements */
    .topbar { padding: 0 12px; justify-content: space-between; }
    .topbar h1 { font-size: 16px; }
    .wa-dot { width: 24px; height: 24px; font-size: 14px; }
    #topbar-user { display: none !important; }
    #last-updated { display: none !important; }
    .refresh-row button { padding: 8px 12px; font-size: 12px; border-radius: 8px; }

    /* Tables to Cards */
    table, thead, tbody, th, td, tr { display: block; width: 100%; box-sizing: border-box; }
    thead { display: none; }
    tr {
      margin-bottom: 16px;
      border: 1px solid #e5e7eb;
      border-radius: 12px;
      padding: 12px;
      background: #fff;
      box-shadow: 0 4px 12px rgba(0,0,0,0.04);
    }
    td {
      display: flex;
      justify-content: space-between;
      align-items: center;
      text-align: right;
      padding: 10px 0;
      border-bottom: 1px solid #f9fafb;
      gap: 12px;
      font-size: 13px;
    }
    td:last-child { border-bottom: none; }
    td::before {
      font-size: 11px;
      font-weight: 700;
      color: #6b7280;
      text-transform: uppercase;
      flex-shrink: 0;
    }
    
    /* Order Table Labels */
    #tab-orders td:nth-of-type(1)::before { content: "Order #"; }
    #tab-orders td:nth-of-type(2)::before { content: "Customer"; }
    #tab-orders td:nth-of-type(3)::before { content: "WhatsApp"; }
    #tab-orders td:nth-of-type(4)::before { content: "Items"; }
    #tab-orders td:nth-of-type(5)::before { content: "Total"; }
    #tab-orders td:nth-of-type(6)::before { content: "Payment"; }
    #tab-orders td:nth-of-type(7)::before { content: "Address"; }
    #tab-orders td:nth-of-type(8)::before { content: "Status"; }
    #tab-orders td:nth-of-type(9)::before { content: "Date"; }
    #tab-orders td:nth-of-type(10)::before { content: "Action"; }

    /* Customers Table Labels */
    #tab-customers td:nth-of-type(1)::before { content: "Name"; }
    #tab-customers td:nth-of-type(2)::before { content: "WhatsApp"; }
    #tab-customers td:nth-of-type(3)::before { content: "CRM Stage"; }
    #tab-customers td:nth-of-type(4)::before { content: "Opt-in"; }
    #tab-customers td:nth-of-type(5)::before { content: "Address"; }
    #tab-customers td:nth-of-type(6)::before { content: "First Seen"; }
    #tab-customers td:nth-of-type(7)::before { content: "Last Contact"; }

    /* Inventory Table Labels */
    #tab-inventory td:nth-of-type(1)::before { content: "Photo"; }
    #tab-inventory td:nth-of-type(2)::before { content: "SKU"; }
    #tab-inventory td:nth-of-type(3)::before { content: "Name"; }
    #tab-inventory td:nth-of-type(4)::before { content: "Price"; }
    #tab-inventory td:nth-of-type(5)::before { content: "Stock"; }
    #tab-inventory td:nth-of-type(6)::before { content: "Status"; }
    #tab-inventory td:nth-of-type(7)::before { content: "Update Stock"; }
    #tab-inventory td:nth-of-type(8)::before { content: "Media URL"; }
    #tab-inventory td:nth-of-type(9)::before { content: "Actions"; }
    
    /* Touch Targets & Base Styles */
    .filter-bar { padding: 16px; background: #fff; }
    .filter-bar select {
      width: 100%;
      padding: 12px;
      margin-bottom: 8px;
      font-size: 14px;
      border-radius: 8px;
      background: #f9fafb;
    }
    button, input { min-height: 44px; }
    .panel { margin: 0; border-radius: 0; box-shadow: none; border-top: 1px solid #f0f0f0; }
    .table-wrap { padding: 16px; background: #f9fafb; overflow-x: hidden; }
    .analytics-grid { padding: 16px; grid-template-columns: 1fr; }
    .analytics-card { grid-column: 1 / -1 !important; margin-bottom: 16px; }
  }
'''

# Replace the old CSS block
pattern_css = r'/\* Modern Mobile App Principles \*/.*?(?=</style>)'
if re.search(pattern_css, content, flags=re.DOTALL):
    content = re.sub(pattern_css, mobile_css, content, flags=re.DOTALL)
    print("Mobile CSS block successfully updated.")
else:
    print("WARNING: Could not find old mobile CSS block.")
    
with open(file_path, 'w', encoding='utf-8') as f:
    f.write(content)

print("Finished applying updates.")
