// Fixed synthetic input. The nested 'value' binding must remain separate.
import { request } from './transport.js';
export function load(value) {
  function nested(value) { return String(value); }
  return request('/fixture', nested(value));
}
