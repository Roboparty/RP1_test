export type PageWithItems<T> = {
  items: T[];
};

export function mergeUniquePages<T>(
  pages: PageWithItems<T>[],
  getKey: (item: T) => string
): T[] {
  const seen = new Set<string>();
  const merged: T[] = [];
  for (const page of pages) {
    for (const item of page.items) {
      const key = getKey(item);
      if (seen.has(key)) continue;
      seen.add(key);
      merged.push(item);
    }
  }
  return merged;
}

export function paginationKey(scope: string, ...identity: Array<string | null | undefined>) {
  return [scope, ...identity.map((value) => value?.trim() ?? "")] as const;
}
