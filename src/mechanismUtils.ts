/**
 * Pure utility functions for the MechanismDict type.
 * Keys are 1-indexed message numbers matching the UI display.
 */

export type MechanismDict = Record<number, string>;

export type MessageOp =
  | { type: 'insert'; index: number }
  | { type: 'delete'; start: number; end: number }
  | { type: 'move'; fromStart: number; fromEnd: number; to: number }
  | { type: 'replace'; start: number; end: number; newCount: number };

/** Convert a MechanismDict to a readable string for backend APIs that expect a string. */
export function mechanismDictToString(dict: MechanismDict | null): string {
  if (!dict || Object.keys(dict).length === 0) return '';
  const entries = Object.entries(dict)
    .map(([k, v]) => [Number(k), v] as [number, string])
    .sort(([a], [b]) => a - b);
  return entries.map(([k, v]) => `[${k}] ${v}`).join('\n');
}

/** Remap mechanism dict keys after a message operation. Returns a new dict. */
export function remapMechanismKeys(dict: MechanismDict | null, op: MessageOp): MechanismDict | null {
  if (!dict || Object.keys(dict).length === 0) return dict;

  const result: MechanismDict = {};

  switch (op.type) {
    case 'insert': {
      // Keys >= index shift +1
      for (const [k, v] of Object.entries(dict)) {
        const key = Number(k);
        if (key >= op.index) {
          result[key + 1] = v;
        } else {
          result[key] = v;
        }
      }
      return result;
    }

    case 'delete': {
      // Keys in [start, end] removed; keys > end shift down by count
      const count = op.end - op.start + 1;
      for (const [k, v] of Object.entries(dict)) {
        const key = Number(k);
        if (key >= op.start && key <= op.end) {
          // Removed — skip
        } else if (key > op.end) {
          result[key - count] = v;
        } else {
          result[key] = v;
        }
      }
      return result;
    }

    case 'move': {
      // Mirror the executor's move logic:
      // 1. Extract keys in [fromStart, fromEnd]
      // 2. Shift remaining keys to close the gap
      // 3. Reinsert at adjusted destination
      const count = op.fromEnd - op.fromStart + 1;
      const extracted: Array<[number, string]> = [];
      const remaining: Array<[number, string]> = [];

      for (const [k, v] of Object.entries(dict)) {
        const key = Number(k);
        if (key >= op.fromStart && key <= op.fromEnd) {
          // Relative offset within the moved block
          extracted.push([key - op.fromStart, v]);
        } else {
          remaining.push([key, v]);
        }
      }

      // Close the gap: keys > fromEnd shift down by count
      const afterRemoval: Array<[number, string]> = remaining.map(([key, v]) => {
        if (key > op.fromEnd) {
          return [key - count, v];
        }
        return [key, v];
      });

      // Adjust destination (same logic as executor)
      let adjustedTo = op.to;
      if (op.to > op.fromEnd) {
        adjustedTo -= count;
      }

      // Insert: keys >= adjustedTo shift up by count
      for (const [key, v] of afterRemoval) {
        if (key >= adjustedTo) {
          result[key + count] = v;
        } else {
          result[key] = v;
        }
      }

      // Place extracted block at adjustedTo
      for (const [offset, v] of extracted) {
        result[adjustedTo + offset] = v;
      }

      return result;
    }

    case 'replace': {
      // Keys in [start, end] removed; keys > end shift by delta
      const removedCount = op.end - op.start + 1;
      const delta = op.newCount - removedCount;
      for (const [k, v] of Object.entries(dict)) {
        const key = Number(k);
        if (key >= op.start && key <= op.end) {
          // Removed — skip
        } else if (key > op.end) {
          result[key + delta] = v;
        } else {
          result[key] = v;
        }
      }
      return result;
    }
  }
}
