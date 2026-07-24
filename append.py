import sys
app_content = open('d:\\PROJETOS SNYKIA\\dashboad whasatpp presidente disel\\app.py', encoding='utf-8').read()
reports_content = open('extracted_reports_clean.py', encoding='utf-8').read()

insert_idx = app_content.find("if __name__ == '__main__':")
if insert_idx != -1:
    new_app = app_content[:insert_idx] + '\n# --- PORTED REPORTS ---\n' + reports_content + '\n# ----------------------\n\n' + app_content[insert_idx:]
    open('d:\\PROJETOS SNYKIA\\dashboad whasatpp presidente disel\\app.py', 'w', encoding='utf-8').write(new_app)
    print('Appended successfully')
else:
    print('Could not find main block')
