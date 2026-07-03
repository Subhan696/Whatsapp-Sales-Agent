import sys

file_path = r'd:\projects\MCP\Whatsapp AI\app\admin\router.py'
with open(file_path, 'r', encoding='utf-8') as f:
    content = f.read()

mobile_css = '''
  /* Modern Mobile App Principles */
  @media (max-width: 768px) {
    /* Bottom Navigation Bar for Tabs */
    .tabs {
      position: fixed;
      bottom: 0;
      left: 0;
      right: 0;
      background: #fff;
      padding: 10px 10px calc(env(safe-area-inset-bottom, 10px) + 10px) 10px;
      box-shadow: 0 -4px 16px rgba(0,0,0,0.06);
      z-index: 9999;
      display: flex;
      justify-content: flex-start;
      border-radius: 0;
      gap: 6px;
      overflow-x: auto;
      -webkit-overflow-scrolling: touch;
    }
    .tabs::-webkit-scrollbar { display: none; }
    .tab {
      flex: 0 0 auto;
      min-width: 65px;
      flex-direction: column;
      padding: 8px 4px;
      font-size: 10px;
      background: transparent;
      color: #9ca3af;
      border-radius: 8px;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 4px;
      border-bottom: none !important;
      text-align: center;
    }
    .tab.active {
      color: #25D366;
      background: #f0fdf4;
      box-shadow: none;
    }
    .tab.active::after { display: none; }
    
    body { padding-bottom: 90px; }
    
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
    .topbar { padding: 0 12px; }
    .topbar h1 { font-size: 14px; }
    .wa-dot { width: 24px; height: 24px; font-size: 14px; }
    #topbar-user { display: none !important; }
    #last-updated { display: none !important; }
    .refresh-row button { padding: 6px 10px; font-size: 11px; border-radius: 6px; }

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
  }
'''

if '/* Modern Mobile App Principles */' in content:
    # Replace from /* Modern Mobile App Principles */ to the next </style>
    # Wait, we need to be careful not to consume the </style> itself, just replace the block.
    import re
    # We replace from /* Modern Mobile ... */ up to but not including </style>
    pattern = r'/\* Modern Mobile App Principles \*/.*?(?=</style>)'
    new_content = re.sub(pattern, mobile_css, content, flags=re.DOTALL)
else:
    # Insert before the first </style>
    parts = content.split('</style>')
    if len(parts) >= 2:
        parts[0] = parts[0] + mobile_css
        new_content = '</style>'.join(parts)
    else:
        new_content = content

with open(file_path, 'w', encoding='utf-8') as f:
    f.write(new_content)

print('Mobile design updated successfully.')
