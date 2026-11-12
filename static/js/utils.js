const formatApiError = (detail) => {
  if (Array.isArray(detail)) {
    return detail.map((item) => item.msg).join(" ");
  }
  return typeof detail === "string" ? detail : "The request could not be completed.";
};

const readApiResponse = async (response) => {
  const text = await response.text();
  let payload = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = { detail: text };
    }
  }

  if (!response.ok) {
    throw new Error(formatApiError(payload?.detail));
  }
  return payload;
};

const showApiMessage = (container, message, type = "danger") => {
  let alert = container.querySelector("[data-api-message]");
  if (!alert) {
    alert = document.createElement("div");
    alert.dataset.apiMessage = "";
    container.prepend(alert);
  }
  alert.className = `alert alert-${type}`;
  alert.setAttribute("role", "alert");
  alert.textContent = message;
};

document.querySelectorAll("form[data-api-url]").forEach((form) => {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();

    const submitButton = form.querySelector('button[type="submit"]');
    const originalButtonText = submitButton?.textContent;
    if (submitButton) {
      submitButton.disabled = true;
      submitButton.textContent = "Please wait...";
    }

    try {
      const payload = Object.fromEntries(new FormData(form).entries());
      const response = await fetch(form.dataset.apiUrl, {
        method: form.dataset.apiMethod,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const result = await readApiResponse(response);

      if (form.dataset.redirectUrl) {
        window.location.assign(form.dataset.redirectUrl);
        return;
      }

      if (form.dataset.redirectTemplate && result?.id) {
        const redirectUrl = new URL(form.dataset.redirectTemplate, window.location.href);
        redirectUrl.pathname = redirectUrl.pathname.replace(
          /\/0(?=\/|$)/,
          `/${encodeURIComponent(result.id)}`,
        );
        window.location.assign(redirectUrl);
        return;
      }
    } catch (error) {
      showApiMessage(form, error instanceof Error ? error.message : "The request failed.");
    } finally {
      if (submitButton) {
        submitButton.disabled = false;
        submitButton.textContent = originalButtonText;
      }
    }
  });
});

document.querySelectorAll("[data-api-read-url]").forEach(async (element) => {
  try {
    const response = await fetch(element.dataset.apiReadUrl);
    const result = await readApiResponse(response);

    if (element.dataset.apiResource === "post") {
      element.querySelector("[data-post-author]").textContent = result.author;
      element.querySelector("[data-post-title]").textContent = result.title;
      element.querySelector("[data-post-content]").textContent = result.content;
      element.querySelector("[data-post-date]").textContent = result.date_posted;
    } else if (element.dataset.apiResource === "user") {
      element.querySelector("[data-user-name]").textContent = result.username;
    }
  } catch (error) {
    showApiMessage(
      element,
      `Could not refresh this information from the API. ${error instanceof Error ? error.message : ""}`,
      "warning",
    );
  }
});