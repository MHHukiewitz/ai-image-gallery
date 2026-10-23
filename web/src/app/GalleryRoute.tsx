import { ImageModal } from "../features/image-detail/ImageModal";
import { ImageGrid } from "../features/gallery/ImageGrid";
import { ShareRequests } from "../features/sharing/ShareRequests";
import { UploadZone } from "../features/upload/UploadZone";

/**
 * The gallery page: requests to share a gallery with the user, upload, the grid, and the modal
 * that the `?image=` param opens over it.
 */
export function GalleryRoute() {
  return (
    <div className="space-y-6">
      <h1 className="sr-only">Your images</h1>
      <ShareRequests />
      <UploadZone />
      <ImageGrid />
      <ImageModal />
    </div>
  );
}
