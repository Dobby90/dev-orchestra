import { Button } from "./ui/button";

export function DesignImageList({
  images,
  onRemove,
  disabled,
}: {
  images: { name: string; src: string }[];
  onRemove?: (src: string) => void;
  disabled?: boolean;
}) {
  if (!images.length) return null;
  return (
    <ul className="design-image-list">
      {images.map(({ name, src }) => (
        <li key={src}>
          <a
            href={src}
            target="_blank"
            rel="noopener"
            aria-label={`${name} 원본 열기`}
          >
            <img src={src} alt="" />
            <span>{name}</span>
          </a>
          {onRemove && (
            <Button
              type="button"
              variant="ghost"
              disabled={disabled}
              onClick={() => onRemove(src)}
              aria-label={`${name} 첨부 제거`}
            >
              제거
            </Button>
          )}
        </li>
      ))}
    </ul>
  );
}
