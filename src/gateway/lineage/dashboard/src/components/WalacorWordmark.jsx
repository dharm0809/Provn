import React from 'react';
import walacorLogoDark from '../assets/branding/walacor-logo-dark.png';
import walacorLogoLight from '../assets/branding/walacor-logo-light.png';

/**
 * Renders Walacor wordmark for current theme: dark-on-clear asset in default
 * (dark) UI, black ink asset when html[data-theme="light"].
 */
export default function WalacorWordmark({ size = 'seal' }) {
  const wrapClass = size === 'eyebrow' ? 'walacor-wordmark walacor-wordmark--eyebrow' : 'walacor-wordmark walacor-wordmark--seal';
  return (
    <span className={wrapClass} role="img" aria-label="Walacor">
      <img
        className="walacor-wordmark__img walacor-wordmark__img--dark"
        src={walacorLogoDark}
        alt=""
        decoding="async"
      />
      <img
        className="walacor-wordmark__img walacor-wordmark__img--light"
        src={walacorLogoLight}
        alt=""
        decoding="async"
      />
    </span>
  );
}
