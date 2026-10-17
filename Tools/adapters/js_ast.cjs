'use strict';
// Parses source; never imports or evaluates the supplied program.
const fs = require('fs');
const path = require('path');
const runtime = process.argv[2];
const input = process.argv[3];
const parser = require(path.join(runtime, 'node_modules/@babel/parser'));
const traverse = require(path.join(runtime, 'node_modules/@babel/traverse')).default;
const source = fs.readFileSync(input, 'utf8');
const ext = path.extname(input).toLowerCase();
const plugins = [];
if (['.jsx', '.tsx'].includes(ext)) plugins.push('jsx');
if (['.ts', '.tsx'].includes(ext)) plugins.push('typescript');
const ast = parser.parse(source, {sourceType: 'unambiguous', plugins, errorRecovery: false});
const functions = [], calls = [], imports = [], bindings = [];
const location = n => ({start: n.start, end: n.end, line: n.loc.start.line, column: n.loc.start.column});
traverse(ast, {
  Function(p) {
    const n = p.node;
    functions.push({name: n.id?.name || p.parent?.id?.name || null, type: n.type, ...location(n)});
  },
  CallExpression(p) {
    const n = p.node;
    calls.push({callee: source.slice(n.callee.start, n.callee.end), ...location(n)});
  },
  ImportDeclaration(p) { imports.push({source: p.node.source.value, ...location(p.node)}); },
  Scopable(p) {
    for (const [name, b] of Object.entries(p.scope.bindings)) {
      if (b.scope !== p.scope) continue;
      bindings.push({name, kind: b.kind, ...location(b.identifier), references: b.referencePaths.map(r => location(r.node))});
    }
  }
});
const uniqueBindings = [...new Map(bindings.map(b => [`${b.start}:${b.name}`, b])).values()];
console.log(JSON.stringify({engine: '@babel/parser + @babel/traverse', versions: {
  parser: require(path.join(runtime, 'node_modules/@babel/parser/package.json')).version,
  traverse: require(path.join(runtime, 'node_modules/@babel/traverse/package.json')).version
}, sourceType: ast.program.sourceType, node_count: (() => {let count = 0; traverse(ast, {enter(){count++;}}); return count;})(),
functions, calls, imports, bindings: uniqueBindings,
limitations: ['Syntax and lexical bindings only; calls are not a resolved call graph.', 'No source execution, runtime values, semantic equivalence, or original names recovery.']}));
