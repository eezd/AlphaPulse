import MarkdownIt from "markdown-it"

// html: false —— 模型输出和 skill 内容里的 HTML 一律按文本显示，不执行
const md = new MarkdownIt({ html: false, linkify: true, breaks: true })

// 链接一律新窗口打开
const defaultLinkOpen = md.renderer.rules.link_open ?? ((tokens, idx, options, _env, self) => self.renderToken(tokens, idx, options))
md.renderer.rules.link_open = (tokens, idx, options, env, self) => {
  tokens[idx].attrSet("target", "_blank")
  tokens[idx].attrSet("rel", "noopener noreferrer")
  return defaultLinkOpen(tokens, idx, options, env, self)
}

export function renderMarkdown(source: string): string {
  return md.render(source)
}
