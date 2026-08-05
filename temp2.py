import re
with open('js/dashboard.js', 'r', encoding='utf-8') as f:
    code = f.read()

replacements = [
    (re.compile(r"    let targetInstance = currentChat\.instance;\n    if \(targetInstance\.startsWith\('inst'\)\) \{\n      targetInstance = getDefaultInstance\(\);\n      if \(!targetInstance\) \{\n        alert\('Nenhuma instância vinculada\.'\);\n        return;\n      \}\n    \}"), "    const targetInstance = resolveTargetInstance(currentChat);\n    if (!targetInstance) {\n      alert('Nenhuma instância configurada');\n      return;\n    }"),
    
    (re.compile(r"      let targetInstance = currentChat\.instance;\n      if \(targetInstance\.startsWith\('inst'\)\) \{\n        targetInstance = getDefaultInstance\(\);\n        if \(!targetInstance\) \{\n          throw new Error\('Nenhuma instância vinculada\.'\);\n        \}\n      \}"), "      const targetInstance = resolveTargetInstance(currentChat);\n      if (!targetInstance) {\n        throw new Error('Nenhuma instância vinculada.');\n      }")
]

for pat, repl in replacements:
    code = pat.sub(repl, code)

with open('js/dashboard.js', 'w', encoding='utf-8') as f:
    f.write(code)
print('Done!')
