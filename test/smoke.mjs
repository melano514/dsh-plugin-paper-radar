// Minimal smoke test: load the plugin with a stub Cordis context and check
// that it registers the `paper-radar` skill with install-time paths resolved.
// Run with: node test/smoke.mjs
import assert from 'node:assert/strict'
import { existsSync, statSync } from 'node:fs'
import { apply, inject, name, renderSkillBody, scriptPath } from '../index.js'

const registered = []
const ctx = {
  skills: {
    register(skill) {
      registered.push(skill)
      return () => {}
    },
  },
}

apply(ctx)

assert.equal(name, 'paper-radar', 'plugin name must be the skill name')
assert.deepEqual(inject, ['skills'], 'the plugin injects only the skills service')
assert.equal(registered.length, 1, 'exactly one skill is registered')

const [skill] = registered
assert.match(skill.name, /^[a-z0-9]+(?:-[a-z0-9]+)*$/)
assert.ok(skill.description.length > 40, 'description must be present')
assert.equal(typeof skill.source, 'string')
assert.equal(typeof skill.whenToUse, 'string')
assert.ok(skill.content.includes(scriptPath), 'skill body must point at the bundled script')
assert.ok(!skill.content.includes('{{SCRIPT}}'), 'no unresolved placeholders')
assert.ok(skill.content.length > 2000, 'skill body must carry the full workflow')
assert.ok(existsSync(scriptPath), 'bundled script must exist')
assert.ok(statSync(scriptPath).size > 20000, 'bundled script must not be a stub')
assert.equal(renderSkillBody('X').includes('X'), true)

console.log(`smoke ok: ${skill.content.length} chars, script ${scriptPath}`)
