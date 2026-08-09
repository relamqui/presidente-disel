import os

html_files = ['dashboard.html', 'index.html', 'admin.html', 'reports.html', 'entregador.html']
favicon_tag = '<link rel="icon" href="/favicon.svg" type="image/svg+xml">'

for filename in html_files:
    if os.path.exists(filename):
        with open(filename, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Check if we already have a favicon link
        if 'rel="icon"' not in content:
            # Insert before </head>
            content = content.replace('</head>', f'    {favicon_tag}\n</head>')
        else:
            # Replace existing
            import re
            content = re.sub(r'<link[^>]*rel=["\']icon["\'][^>]*>', favicon_tag, content)
            
        with open(filename, 'w', encoding='utf-8') as f:
            f.write(content)
print('Favicon updated in all HTML files')
