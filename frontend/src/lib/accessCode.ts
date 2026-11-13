/** The live-analysis access code, remembered in this browser only.
 *
 *  Storage can be missing or refuse (a private window, blocked site data); then the code
 *  simply lasts as long as the page, and the panel asks again next time.
 */

const KEY = 'traffic.liveAccessCode';
let inMemory: string | null = null;

export function readAccessCode(): string | null {
  try {
    return window.localStorage.getItem(KEY) ?? inMemory;
  } catch {
    return inMemory;
  }
}

export function saveAccessCode(code: string): void {
  inMemory = code;
  try {
    window.localStorage.setItem(KEY, code);
  } catch {
    // kept in memory for this page
  }
}

export function forgetAccessCode(): void {
  inMemory = null;
  try {
    window.localStorage.removeItem(KEY);
  } catch {
    // nothing stored
  }
}
