export const repo = 'https://github.com/emeieiron/fe-codec';

/** A path under the site's base, which is `/fe-codec/` on GitHub Pages. */
export function url(path = ''): string {
  const base = import.meta.env.BASE_URL.replace(/\/?$/, '/');
  return base + path.replace(/^\//, '');
}

export interface NavItem {
  slug: string;
  label: string;
}

export interface NavGroup {
  label: string;
  items: NavItem[];
}

/** The sidebar, in reading order. Every page in docs/ must appear here. */
export const nav: NavGroup[] = [
  {
    label: 'Start',
    items: [
      { slug: 'getting-started', label: 'Getting started' },
      { slug: 'types', label: 'Types and limits' },
    ],
  },
  {
    label: 'Use',
    items: [
      { slug: 'generator', label: 'The generator' },
      { slug: 'eip712', label: 'EIP-712' },
    ],
  },
  {
    label: 'Extend',
    items: [
      { slug: 'writing-serializers', label: 'Writing serializers' },
      { slug: 'writing-a-format', label: 'Writing a format' },
    ],
  },
  {
    label: 'Reference',
    items: [
      { slug: 'formats', label: 'Wire profiles' },
      { slug: 'design', label: 'Design and guarantees' },
    ],
  },
];

export const pages: NavItem[] = nav.flatMap((group) => group.items);
