/** The hamburger button. The icon (public/menu.svg) is a CSS mask, so it takes the text color in both themes. */
export default function MenuButton({ onClick }: { onClick: () => void }) {
  return (
    <button type="button" className="menu-btn" aria-label="Menu" onClick={onClick}>
      <span className="menu-icon" />
    </button>
  );
}
