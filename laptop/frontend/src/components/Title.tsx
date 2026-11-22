import titleDark from "../assets/title-dark.png";
import titleLight from "../assets/title-light.png";

/** The "Ratatouille" title. Both versions are in the page; CSS shows the one for the current theme (data-theme). */
export default function Title() {
  return (
    <div className="title">
      <img className="title__light" src={titleLight} alt="Ratatouille" />
      <img className="title__dark" src={titleDark} alt="" aria-hidden="true" />
    </div>
  );
}
