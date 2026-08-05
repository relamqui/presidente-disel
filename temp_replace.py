import re
with open('js/dashboard.js', 'r', encoding='utf-8') as f:
    code = f.read()

helper_func = '''
function resolveTargetInstance(chat) {
    let targetInstance = chat.instance || chat.instanceName;
    try {
        const userData = JSON.parse(localStorage.getItem('wp_crm_user') || '{}');
        if (userData.instances && userData.instances.length > 0) {
            return userData.instances[0];
        }
    } catch(e) {}
    
    if (!targetInstance || targetInstance.startsWith('inst')) {
        return getDefaultInstance();
    }
    return targetInstance;
}
'''

if 'function resolveTargetInstance' not in code:
    code = code.replace("function getDefaultInstance() {\n    return 'presidente';\n}", "function getDefaultInstance() {\n    return 'presidente';\n}\n" + helper_func)


code = code.replace("""    // Se a instância for mock (inst1, inst2...), tenta pegar uma real
    let targetInstance = currentChat.instance;
    if (targetInstance.startsWith('inst')) {
       // Busca primeiro nome de instância real que o usuário tem
       targetInstance = getDefaultInstance();
       if (!targetInstance) {
         throw new Error('Nenhuma instância do WhatsApp vinculada a este usuário.');
       }
    }""", """    const targetInstance = resolveTargetInstance(currentChat);
    if (!targetInstance) {
      throw new Error('Nenhuma instância do WhatsApp vinculada a este usuário.');
    }""")

code = code.replace("""    // Fallback de instância
    let targetInstance = currentChat.instance;
    if (targetInstance.startsWith('inst')) {
      targetInstance = getDefaultInstance();
      if (!targetInstance) {
        alert('Nenhuma instância configurada');
        return;
      }
    }""", """    const targetInstance = resolveTargetInstance(currentChat);
    if (!targetInstance) {
      alert('Nenhuma instância configurada');
      return;
    }""")

code = code.replace("""      let targetInstance = currentChat.instance;
      if (targetInstance.startsWith('inst')) {
        targetInstance = getDefaultInstance();
        if (!targetInstance) {
          showToast('Nenhuma instância válida para enviar arquivo.');
          return;
        }
      }""", """      const targetInstance = resolveTargetInstance(currentChat);
      if (!targetInstance) {
        showToast('Nenhuma instância válida para enviar arquivo.');
        return;
      }""")

code = code.replace("""      let targetInstance = currentChat.instance;
      if (targetInstance.startsWith('inst')) {
        targetInstance = getDefaultInstance();
        if (!targetInstance) {
          showToast('Nenhuma instância configurada para enviar vídeo.');
          return;
        }
      }""", """      const targetInstance = resolveTargetInstance(currentChat);
      if (!targetInstance) {
        showToast('Nenhuma instância configurada para enviar vídeo.');
        return;
      }""")

code = code.replace("""    // Resolve instância antes para poder montar o DOC_REF temporário
    let targetInstance = currentChat.instance;
    if (targetInstance.startsWith('inst')) {
      targetInstance = getDefaultInstance();
      if (!targetInstance) {
        showToast('Nenhuma instância vinculada.');
        return;
      }
    }""", """    const targetInstance = resolveTargetInstance(currentChat);
    if (!targetInstance) {
      showToast('Nenhuma instância configurada.');
      return;
    }""")

code = code.replace("const targetInstance = currentChat.instanceName || currentChat.instance;", "const targetInstance = resolveTargetInstance(currentChat);")


with open('js/dashboard.js', 'w', encoding='utf-8') as f:
    f.write(code)
print("Done")
