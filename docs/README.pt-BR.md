<p align="center">
  <img src="../assets/branding/clarify-logo.png" alt="Logo do ClarifyVoice" width="112">
</p>

# ClarifyVoice

[English](../README.md) · [Instalação](#instalação-no-windows) ·
[Como contribuir](../CONTRIBUTING.md) · [Segurança](../SECURITY.md)

O ClarifyVoice é um assistente desktop leve que transforma voz em texto bem
escrito dentro de qualquer aplicativo do Windows. Ele também reescreve e traduz
textos selecionados usando Gemini, OpenAI, Groq ou endpoints compatíveis.

## Principais recursos

- Transcrição e refinamento de voz com atalhos globais
- Reescrita segura de texto selecionado com verificação de foco
- Tradução de texto selecionado
- Integração nativa com atalhos e bandeja do Windows
- Interface em inglês, português, espanhol, alemão e russo
- Estatísticas locais sem armazenar o conteúdo das transcrições
- Sem conta ClarifyVoice, backend próprio ou telemetria

> [!IMPORTANT]
> Você precisa fornecer uma chave de API de pelo menos um provedor. As chaves e
> estatísticas ficam no seu computador. O áudio e o texto selecionado são
> enviados diretamente ao provedor configurado.

## Instalação no Windows

### Instalador Windows (em preparação)

O repositório já contém o contrato fail-closed do MSI e da atualização
autenticada, mas o recurso não deve ser publicado ou considerado pronto antes
dos gates de assinatura gerenciada, armazenamento seguro de credenciais,
proveniência e validação manual. Quando uma release futura incluir o arquivo
`ClarifyVoice-windows-x64.msi`, instale somente se o publisher Authenticode e o
SHA-256 corresponderem à release. Consulte [segurança da distribuição e das
atualizações](windows-distribution.md) para os comportamentos de instalação,
upgrade, reparo, rollback e desinstalação.

### Executável portátil

1. Abra a [versão mais recente](https://github.com/jvictormaynard/clarify-voice/releases/latest).
2. Baixe `ClarifyVoice.exe` e salve-o em uma pasta sob seu controle.
3. Abra o executável.
4. Entre em **Models**, adicione sua chave, valide o provedor e escolha os
   modelos de transcrição e refinamento.

Os executáveis publicados até a v0.1.2 ainda não possuem assinatura de código. Por isso, o Windows
SmartScreen pode pedir confirmação no primeiro uso. Cada release inclui um
arquivo SHA-256 para conferir a integridade do download.

Se ainda não houver uma release, instale pelo código-fonte:

```powershell
git clone https://github.com/jvictormaynard/clarify-voice.git
cd clarify-voice
.\start.bat
```

É necessário ter Windows 10 ou 11, Python 3.11 ou mais recente e um microfone.
Na primeira execução, o script cria um ambiente virtual e instala as
dependências automaticamente.

## Atalhos

| Atalho | Ação |
| --- | --- |
| `Alt + L` | Iniciar ou encerrar a gravação |
| `Esc` | Cancelar a gravação ativa |
| `Alt + K` | Reescrever o texto selecionado |
| `Alt + T` | Traduzir o texto selecionado |
| `Alt + R` | Mostrar ou esconder o ClarifyVoice |

## Privacidade

O ClarifyVoice não possui servidor próprio. As configurações e estatísticas
locais ficam em `%APPDATA%\ClarifyVoice`. No Windows, as chaves ficam separadas
em `secrets.dpapi.json`, criptografadas pela DPAPI para o usuário atual. Chaves
antigas em texto simples são migradas e só são removidas de `config.json` depois
da confirmação da cópia protegida. Variáveis de ambiente são substituições
temporárias e não são persistidas.

Excluir somente o executável não apaga os dados. Para remover também as
credenciais, exclua `secrets.dpapi.json` ou toda a pasta de dados do
ClarifyVoice. Em execuções experimentais no Linux/macOS, `secrets.json` é um
fallback em texto simples com permissões restritas; não compartilhe esse arquivo.

## Desenvolvimento e contribuição

A documentação técnica principal está em inglês para facilitar a colaboração
internacional:

- [Guia de contribuição](../CONTRIBUTING.md)
- [Ambiente de desenvolvimento](development.md)
- [Arquitetura](architecture.md)
- [Dicionário local e snippets](dictionary-snippets.md)
- [Microfones e limites de gravação](microphone-controls.md)
- [Suporte](../SUPPORT.md)
- [Política de segurança](../SECURITY.md)

O código do ClarifyVoice usa a [Licença MIT](../LICENSE). O SoX e outras
dependências mantêm suas próprias licenças, documentadas em
[THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).
