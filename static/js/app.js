(() => {
  const summary = document.querySelector("[data-error-summary]");
  if (summary) summary.focus();

  document.querySelectorAll("[data-camera-control]").forEach((control) => {
    const input = control.querySelector('input[type="file"]');
    const name = control.querySelector("[data-file-name]");
    const preview = control.querySelector("[data-preview]");
    if (!input || !name || !preview) return;
    input.addEventListener("change", () => {
      const files = Array.from(input.files || []);
      if (!files.length) {
        name.textContent = "No photo selected";
        preview.style.backgroundImage = "";
        return;
      }
      name.textContent = files.length === 1 ? files[0].name : `${files.length} photos selected`;
      const first = files[0];
      if (first.type.startsWith("image/")) {
        const url = URL.createObjectURL(first);
        preview.style.backgroundImage = `url("${url}")`;
        preview.setAttribute("aria-hidden", "false");
        setTimeout(() => URL.revokeObjectURL(url), 60000);
      }
    });
  });

  const STAGED_PHOTO_TARGET_BYTES = 3_400_000;

  const canvasBlob = (canvas, quality) =>
    new Promise((resolve, reject) => {
      canvas.toBlob(
        (blob) => (blob ? resolve(blob) : reject(new Error("This browser could not prepare the photo."))),
        "image/jpeg",
        quality,
      );
    });

  const decodePhoto = async (file) => {
    if ("createImageBitmap" in window) {
      try {
        return await createImageBitmap(file, { imageOrientation: "from-image" });
      } catch (_error) {
        // Older Safari releases reject the options object; the Image fallback
        // below still handles the common JPEG/PNG/WebP camera formats.
      }
    }
    const objectUrl = URL.createObjectURL(file);
    try {
      return await new Promise((resolve, reject) => {
        const image = new Image();
        image.onload = () => resolve(image);
        image.onerror = () => reject(new Error("This photo format cannot be prepared here."));
        image.src = objectUrl;
      });
    } finally {
      URL.revokeObjectURL(objectUrl);
    }
  };

  const preparePhoto = async (file) => {
    if (file.size <= STAGED_PHOTO_TARGET_BYTES) return file;
    if (!/^(image\/jpeg|image\/png|image\/webp)$/.test(file.type)) {
      throw new Error(
        "This photo is too large for secure upload. Choose a JPEG, PNG, or WebP smaller than 3.5 MB.",
      );
    }

    const image = await decodePhoto(file);
    const sourceWidth = image.width || image.naturalWidth;
    const sourceHeight = image.height || image.naturalHeight;
    let scale = Math.min(1, 2800 / Math.max(sourceWidth, sourceHeight));
    let smallest = null;

    try {
      for (let pass = 0; pass < 4; pass += 1) {
        const canvas = document.createElement("canvas");
        canvas.width = Math.max(1, Math.round(sourceWidth * scale));
        canvas.height = Math.max(1, Math.round(sourceHeight * scale));
        const context = canvas.getContext("2d", { alpha: false });
        if (!context) throw new Error("This browser could not prepare the photo.");
        context.fillStyle = "#ffffff";
        context.fillRect(0, 0, canvas.width, canvas.height);
        context.drawImage(image, 0, 0, canvas.width, canvas.height);

        for (const quality of [0.9, 0.82, 0.74, 0.66]) {
          const blob = await canvasBlob(canvas, quality);
          smallest = blob;
          if (blob.size <= STAGED_PHOTO_TARGET_BYTES) {
            const baseName = file.name.replace(/\.[^.]+$/, "") || "photo";
            return new File([blob], `${baseName}.jpg`, {
              type: "image/jpeg",
              lastModified: file.lastModified,
            });
          }
        }
        scale *= 0.78;
      }
    } finally {
      if (typeof image.close === "function") image.close();
    }

    if (!smallest || smallest.size > STAGED_PHOTO_TARGET_BYTES) {
      throw new Error("This photo could not be reduced enough. Retake it at the standard camera size.");
    }
    return new File([smallest], "photo.jpg", { type: "image/jpeg" });
  };

  document.querySelectorAll("[data-upload-form]").forEach((form) => {
    const button = form.querySelector("[data-submit-button]");
    const status = form.querySelector("[data-upload-status]");
    const stageUrl = form.dataset.stageUploadUrl;
    const submissionKind = form.dataset.submissionKind;
    const csrf = form.querySelector('input[name="csrfmiddlewaretoken"]');
    const stagedInput = form.querySelector('input[name="staged_uploads"]');
    const progress = new WeakMap();

    const setBusy = (message) => {
      if (button) {
        button.disabled = true;
        button.textContent = message;
      }
      if (status) status.textContent = message;
    };

    const setError = (message) => {
      if (button) {
        button.disabled = false;
        button.textContent = "Try upload again";
      }
      if (status) status.textContent = message;
    };

    form.addEventListener("submit", async (event) => {
      if (!stageUrl || !submissionKind || !stagedInput || !csrf) {
        if (!button || button.disabled) return;
        button.disabled = true;
        button.textContent = "Saving photos…";
        return;
      }
      if (form.dataset.finalSubmit === "true") return;

      event.preventDefault();
      if (button?.disabled) return;

      let staged = {};
      try {
        staged = stagedInput.value ? JSON.parse(stagedInput.value) : {};
        if (!staged || Array.isArray(staged) || typeof staged !== "object") staged = {};
      } catch (_error) {
        staged = {};
      }

      const inputs = Array.from(form.querySelectorAll('input[type="file"]'));
      const pendingCount = inputs.reduce((total, input) => {
        const files = Array.from(input.files || []);
        const completed = progress.get(input)?.completed || 0;
        return total + Math.max(0, files.length - completed);
      }, 0);
      let current = 0;

      try {
        for (const input of inputs) {
          const files = Array.from(input.files || []);
          if (!files.length) continue;

          const signature = files
            .map((file) => `${file.name}:${file.size}:${file.lastModified}`)
            .join("|");
          let state = progress.get(input);
          if (!state || state.signature !== signature) {
            state = { signature, completed: 0 };
            progress.set(input, state);
            // A new browser selection means replacement, for both a single
            // slot and a multi-page invoice. An unchanged signature is the
            // retry path and retains IDs that already reached the server.
            staged[input.name] = [];
          }

          const maximum = Number(input.dataset.maxFiles || (input.multiple ? 12 : 1));
          const alreadyStaged = Array.isArray(staged[input.name]) ? staged[input.name].length : 0;
          if (alreadyStaged + files.length - state.completed > maximum) {
            throw new Error(`Choose no more than ${maximum} photo${maximum === 1 ? "" : "s"} for this slot.`);
          }

          for (let index = state.completed; index < files.length; index += 1) {
            current += 1;
            setBusy(`Preparing photo ${current} of ${pendingCount}…`);
            const prepared = await preparePhoto(files[index]);
            setBusy(`Uploading photo ${current} of ${pendingCount}…`);

            const body = new FormData();
            body.append("csrfmiddlewaretoken", csrf.value);
            body.append("submission_kind", submissionKind);
            body.append("field_name", input.name);
            body.append("photo", prepared, prepared.name);
            const response = await fetch(stageUrl, {
              method: "POST",
              credentials: "same-origin",
              headers: { Accept: "application/json" },
              body,
            });
            let payload = {};
            try {
              payload = await response.json();
            } catch (_error) {
              // A proxy-generated size or session error may not be JSON.
            }
            if (!response.ok || !payload.stage_id) {
              throw new Error(payload.error || "The photo upload was interrupted. Try again.");
            }

            if (!Array.isArray(staged[input.name])) staged[input.name] = [];
            staged[input.name].push(payload.stage_id);
            stagedInput.value = JSON.stringify(staged);
            state.completed = index + 1;
          }
          input.value = "";
          progress.delete(input);
        }

        setBusy("Saving report…");
        form.dataset.finalSubmit = "true";
        HTMLFormElement.prototype.submit.call(form);
      } catch (error) {
        setError(error instanceof Error ? error.message : "The upload was interrupted. Try again.");
      }
    });
  });

  const parseMoneyCents = (raw) => {
    const value = String(raw || "").trim();
    if (!/^\d+(?:\.\d{0,2})?$/.test(value)) return null;
    const [whole, decimal = ""] = value.split(".");
    const cents = Number(whole) * 100 + Number(decimal.padEnd(2, "0"));
    return Number.isSafeInteger(cents) ? cents : null;
  };

  const money = (cents) =>
    new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(
      Math.abs(cents) / 100,
    );

  document.querySelectorAll("[data-daily-cash-form]").forEach((form) => {
    const input = form.querySelector('input[name="amount"]');
    const preview = form.querySelector("[data-daily-cash-preview]");
    const expected = Number(form.dataset.expectedCents);
    if (!input || !preview || !Number.isSafeInteger(expected)) return;

    const updatePreview = () => {
      preview.classList.remove("is-match", "has-issue", "is-invalid");
      if (!input.value.trim()) {
        preview.textContent = "Type the total from this date's pouch.";
        return;
      }
      const counted = parseMoneyCents(input.value);
      if (counted === null) {
        preview.classList.add("is-invalid");
        preview.textContent = "Use dollars and no more than two decimal places.";
        return;
      }
      const variance = counted - expected;
      if (variance === 0) {
        preview.classList.add("is-match");
        preview.textContent = `Match — this pouch should contain ${money(expected)}.`;
      } else {
        preview.classList.add("has-issue");
        preview.textContent = `${variance < 0 ? "Short" : "Over"} ${money(variance)}. You can save this amount, recount, and correct it later.`;
      }
    };

    input.addEventListener("input", updatePreview);
    input.addEventListener("blur", updatePreview);
    updatePreview();
  });
})();
