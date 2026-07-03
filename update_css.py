import sys
import re

file_path = r'd:\projects\MCP\Whatsapp AI\app\admin\router.py'
with open(file_path, 'r', encoding='utf-8') as f:
    content = f.read()

# We need to add the analytics fix to our mobile CSS.
# Let's find our previously injected CSS block.

pattern = r'(/\* Modern Mobile App Principles \*/.*?)(?=</style>)'
match = re.search(pattern, content, flags=re.DOTALL)

if match:
    old_css = match.group(1)
    
    # We want to add `.analytics-card { grid-column: 1 / -1 !important; }` inside the @media query.
    # The end of our mobile_css block has:
    #     .analytics-grid { padding: 16px; grid-template-columns: 1fr; }
    #   }
    
    if '.analytics-card { grid-column: 1 / -1 !important; }' not in old_css:
        new_css = old_css.replace(
            '.analytics-grid { padding: 16px; grid-template-columns: 1fr; }',
            '.analytics-grid { padding: 16px; grid-template-columns: 1fr; }\n    .analytics-card { grid-column: 1 / -1 !important; }'
        )
        
        new_content = content.replace(old_css, new_css)
        
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(new_content)
        print('Analytics layout fix applied.')
    else:
        print('Analytics fix already present.')
else:
    print('Could not find mobile css block.')
