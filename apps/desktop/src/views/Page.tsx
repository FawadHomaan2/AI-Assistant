import type { ReactNode } from 'react';
import { Icon } from '@/components/Icon';
import './views.css';

export function Page({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle?: string;
  children: ReactNode;
}) {
  return (
    <div className="page">
      <header className="page__head">
        <h1>{title}</h1>
        {subtitle && <p>{subtitle}</p>}
      </header>
      <div className="page__body">{children}</div>
    </div>
  );
}

/** Neutral informational block — used to explain scope and limits. */
export function Explainer({ children }: { children: ReactNode }) {
  return (
    <div className="explainer">
      <Icon name="info" size={15} />
      <p>{children}</p>
    </div>
  );
}

/**
 * Marks a surface whose backing implementation does not exist yet, and says
 * which phase delivers it. Used instead of showing placeholder data.
 */
export function NotImplemented({
  phase,
  what,
  items,
}: {
  phase: number;
  what: string;
  items: string[];
}) {
  return (
    <div className="notimpl">
      <header className="notimpl__head">
        <Icon name="alert" size={15} />
        <h2>Not implemented — Phase {phase}</h2>
      </header>
      <p className="notimpl__what">{what}</p>
      <ul className="notimpl__list">
        {items.map((i) => (
          <li key={i}>{i}</li>
        ))}
      </ul>
      <p className="notimpl__note">
        Nothing above is running yet. This panel exists so the interface is
        reviewable now, and it shows no values rather than placeholder ones.
      </p>
    </div>
  );
}

export function Card({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="card">
      <h2 className="card__title">{title}</h2>
      {children}
    </section>
  );
}
