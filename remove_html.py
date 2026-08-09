import os

files_to_check = ['js/login.js', 'js/dashboard.js', 'dashboard.html', 'index.html', 'admin.html', 'reports.html']
for filepath in files_to_check:
    if os.path.exists(filepath):
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
        
        content = content.replace("window.location.href = 'dashboard.html'", "window.location.href = 'dashboard'")
        content = content.replace("window.location.href = 'index.html'", "window.location.href = 'index'")
        content = content.replace("href=\"dashboard.html\"", "href=\"dashboard\"")
        content = content.replace("href=\"index.html\"", "href=\"index\"")
        
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)
print('Replaced .html references')
