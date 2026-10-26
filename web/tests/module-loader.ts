import { existsSync, readFileSync } from "node:fs"
import { createRequire, Module } from "node:module"
import { dirname, resolve } from "node:path"
import { runInNewContext } from "node:vm"
import ts from "typescript"
import { compileScript, parse } from "vue/compiler-sfc"

export const require = createRequire(import.meta.url)
const source = new URL("../src/", import.meta.url).pathname

/** 使用现有 TS/Vue 编译器执行真实模块；只替换 I/O 和浏览器生命周期。 */
export function moduleLoader(mocks: Record<string, unknown>, globals: Record<string, unknown> = {}) {
  const cache = new Map<string, Module>()
  function load(name: string, parent = source): any {
    if (name in mocks) return mocks[name]
    if (!name.startsWith("@/") && !name.startsWith("@@/") && !name.startsWith(".")) return require(name)
    let filename = name.startsWith("@@/")
      ? resolve(source, "common", name.slice(3))
      : name.startsWith("@/") ? resolve(source, name.slice(2)) : resolve(parent, name)
    if (!/\.(?:vue|ts)$/.test(filename)) filename = existsSync(`${filename}.ts`) ? `${filename}.ts` : resolve(filename, "index.ts")
    if (cache.has(filename)) return cache.get(filename)!.exports
    const mod = new Module(filename)
    cache.set(filename, mod)
    let script = readFileSync(filename, "utf8")
    if (filename.endsWith(".vue")) script = compileScript(parse(script).descriptor, { id: filename }).content
    const code = ts.transpileModule(script, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
    runInNewContext(code, {
      module: mod,
      exports: mod.exports,
      require: (value: string) => load(value, dirname(filename)),
      console,
      Error,
      setTimeout,
      clearTimeout,
      AbortController,
      TextDecoder,
      URL,
      ...globals
    }, { filename })
    return mod.exports
  }
  return load
}
