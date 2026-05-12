const browserPath = document.getElementById("browser-path");
const browserStatus = document.getElementById("browser-status");
const browserList = document.getElementById("browser-list");
const browserUpButton = document.getElementById("browser-up-button");
const activityPanel = document.getElementById("activity-panel");
const activityTitle = document.getElementById("activity-title");
const activityPhase = document.getElementById("activity-phase");
const activityProgressBar = document.getElementById("activity-progress-bar");
const activityMessage = document.getElementById("activity-message");
const activityCount = document.getElementById("activity-count");
const activityReports = document.getElementById("activity-reports");
const activityErrors = document.getElementById("activity-errors");
const statusMessage = document.getElementById("status-message");
const resultsTitle = document.getElementById("results-title");
const stats = document.getElementById("stats");
const bulkEditor = document.getElementById("bulk-editor");
const albumArtistInput = document.getElementById("album-artist-input");
const updateAlbumArtistButton = document.getElementById("update-album-artist-button");
const albumTitleInput = document.getElementById("album-title-input");
const updateAlbumTitleButton = document.getElementById("update-album-title-button");
const table = document.getElementById("results-table");
const resultsBody = document.getElementById("results-body");
const emptyArtworkTemplate = document.getElementById("empty-artwork-template");

const state = {
  currentSource: "",
  currentRelativePath: "",
  editable: false,
  sourceType: "",
  browserPath: "",
  browserParentPath: "",
  currentJobId: "",
  pollTimer: null,
  processedFolders: new Set(),
};

function setStatus(message, isError = false) {
  statusMessage.textContent = message;
  statusMessage.style.color = isError ? "#8c1c13" : "";
}

function formatDuration(seconds) {
  if (seconds === null || seconds === undefined) {
    return "—";
  }

  const totalSeconds = Math.round(Number(seconds));
  const minutes = Math.floor(totalSeconds / 60);
  const remainingSeconds = totalSeconds % 60;
  return `${minutes}:${String(remainingSeconds).padStart(2, "0")}`;
}

function renderArtworkCell(item) {
  if (!item.artwork_data_url) {
    return emptyArtworkTemplate.content.firstElementChild.cloneNode(true);
  }

  const image = document.createElement("img");
  image.className = "artwork";
  image.src = item.artwork_data_url;
  image.alt = `${item.title || item.file_name} artwork`;
  return image;
}

function groupItemsByAlbum(items) {
  const groups = [];
  const lookup = new Map();

  items.forEach((item) => {
    const key = item.album_group_key || `${item.album_artist}::${item.album}::${item.album_dir_relative}`;
    if (!lookup.has(key)) {
      const group = {
        key,
        album: item.album || "Unknown Album",
        albumArtist: item.album_artist || "Unknown Artist",
        albumDirRelative: item.album_dir_relative || "",
        artworkItem: item,
        tracks: [],
      };
      lookup.set(key, group);
      groups.push(group);
    }
    lookup.get(key).tracks.push(item);
  });

  return groups;
}

async function moveAlbumToLibrary(albumDirRelative, albumTitle, albumArtist) {
  if (!state.currentRelativePath) {
    setStatus("No processed folder is active.", true);
    return;
  }

  const shouldContinue = window.confirm(
    `Move "${albumArtist} / ${albumTitle}" to the library share and rename tracks to "<track no> - <title>"?`,
  );
  if (!shouldContinue) {
    return;
  }

  const response = await fetch("/api/move-album", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      relative_path: state.currentRelativePath,
      album_dir_relative: albumDirRelative,
    }),
  });

  const payload = await parseResponse(response);
  beginJobTracking(payload.job_id, `Moving "${albumTitle}" to the library share...`);
}

function renderTable(items, sourceLabel) {
  resultsBody.innerHTML = "";

  if (!items.length) {
    table.hidden = true;
    stats.hidden = true;
    resultsTitle.textContent = sourceLabel;
    setStatus("No supported audio files were found in that directory.");
    return;
  }

  const fragment = document.createDocumentFragment();
  const groups = groupItemsByAlbum(items);
  groups.forEach((group) => {
    group.tracks.forEach((item, trackIndex) => {
      const row = document.createElement("tr");
      row.className = trackIndex === 0 ? "album-group-start" : "";
      if (item.is_missing) {
        row.classList.add("track-missing");
        row.style.textDecoration = "line-through";
        row.style.opacity = "0.5";
      }

      if (trackIndex === 0) {
        const artworkCell = document.createElement("td");
        artworkCell.rowSpan = group.tracks.length;
        artworkCell.className = "album-artwork-cell";
        artworkCell.appendChild(renderArtworkCell(group.artworkItem));
        row.appendChild(artworkCell);
      }

      const fileCell = document.createElement("td");
      const name = document.createElement("div");
      name.className = "file-name";
      name.textContent = item.file_name;
      const relativePath = document.createElement("div");
      relativePath.className = "file-path";
      relativePath.textContent = item.relative_path;
      fileCell.append(name, relativePath);
      row.appendChild(fileCell);

      const titleCell = document.createElement("td");
      titleCell.textContent = item.title || "—";
      row.appendChild(titleCell);

      const artistCell = document.createElement("td");
      artistCell.textContent = item.artist || "—";
      row.appendChild(artistCell);

      const albumArtistCell = document.createElement("td");
      albumArtistCell.textContent = item.album_artist || "—";
      row.appendChild(albumArtistCell);

      if (trackIndex === 0) {
        const albumCell = document.createElement("td");
        albumCell.rowSpan = group.tracks.length;
        albumCell.className = "album-cell";

        const albumName = document.createElement("div");
        albumName.className = "album-name";
        albumName.textContent = group.album;

        const albumArtistMeta = document.createElement("div");
        albumArtistMeta.className = "album-meta";
        albumArtistMeta.textContent = group.albumArtist;

        const moveButton = document.createElement("button");
        moveButton.type = "button";
        moveButton.className = "button button-small album-move-button";
        moveButton.textContent = "Move To Library";
        moveButton.addEventListener("click", async () => {
          try {
            await moveAlbumToLibrary(group.albumDirRelative, group.album, group.albumArtist);
          } catch (error) {
            setStatus(error.message, true);
          }
        });

        albumCell.append(albumName, albumArtistMeta, moveButton);

        if (group.tracks[0] && group.tracks[0].mb_lookup_failed) {
          const mbWarning = document.createElement("div");
          mbWarning.className = "album-meta";
          mbWarning.style.color = "#8c1c13";
          mbWarning.style.marginTop = "8px";
          mbWarning.textContent = "⚠️ Album not found on MusicBrainz. Completeness cannot be verified.";
          albumCell.appendChild(mbWarning);
        }

        row.appendChild(albumCell);
      }

      const tailValues = [
        item.disc || "—",
        item.genre || "—",
        item.track || "—",
        item.year || "—",
        formatDuration(item.duration_seconds),
        item.bitrate_kbps ? `${item.bitrate_kbps} kbps` : "—",
        item.sample_rate_hz ? `${item.sample_rate_hz} Hz` : "—",
      ];

      tailValues.forEach((value) => {
        const cell = document.createElement("td");
        cell.textContent = value;
        row.appendChild(cell);
      });

      fragment.appendChild(row);
    });
  });

  resultsBody.appendChild(fragment);
  resultsTitle.textContent = sourceLabel;
  stats.textContent = `${groups.length} album${groups.length === 1 ? "" : "s"} • ${items.length} audio file${items.length === 1 ? "" : "s"}`;
  stats.hidden = false;
  table.hidden = false;
}

function updateBulkEditor(payload) {
  state.currentSource = payload.source || "";
  state.currentRelativePath = payload.source_relative_path || "";
  state.editable = Boolean(payload.editable);
  state.sourceType = payload.source_type || "";

  if (state.currentRelativePath) {
    state.processedFolders.add(state.currentRelativePath);
  }

  bulkEditor.hidden = !state.editable;
  if (!state.editable) {
    albumArtistInput.value = "";
    albumTitleInput.value = "";
  }
}

function renderActivity(job) {
  activityPanel.hidden = false;
  activityTitle.textContent = job.source || "Remote job";
  activityPhase.textContent = job.phase || job.status || "running";
  activityMessage.textContent = job.message || "";
  activityCount.textContent = `${job.progress_current || 0} / ${job.progress_total || 0}`;
  activityProgressBar.style.width = `${job.progress_percent || 0}%`;

  if (job.reports && job.reports.length) {
    activityReports.hidden = false;
    activityReports.innerHTML = "";
    job.reports.forEach((report) => {
      const item = document.createElement("div");
      item.textContent = report;
      activityReports.appendChild(item);
    });
  } else {
    activityReports.hidden = true;
    activityReports.innerHTML = "";
  }

  if (job.errors && job.errors.length) {
    activityErrors.hidden = false;
    activityErrors.innerHTML = "";
    job.errors.forEach((error) => {
      const item = document.createElement("div");
      item.textContent = error;
      activityErrors.appendChild(item);
    });
  } else {
    activityErrors.hidden = true;
    activityErrors.innerHTML = "";
  }
}

async function parseResponse(response) {
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error || "Request failed");
  }
  return payload;
}

function stopJobPolling() {
  if (state.pollTimer) {
    clearTimeout(state.pollTimer);
    state.pollTimer = null;
  }
}

function focusActivityPanel() {
  activityPanel.hidden = false;
  activityPanel.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function pollJob(jobId) {
  const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}`);
  const payload = await parseResponse(response);
  renderActivity(payload);

  if (payload.status === "completed") {
    stopJobPolling();
    updateBulkEditor(payload);
    renderTable(payload.items || [], payload.source || "Processed folder");
    if (payload.job_type === "album_artist_update") {
      setStatus(`Updated album artist on ${payload.updated_count || 0} audio file${payload.updated_count === 1 ? "" : "s"}.`);
    } else if (payload.job_type === "album_title_update") {
      setStatus(`Updated album title on ${payload.updated_count || 0} audio file${payload.updated_count === 1 ? "" : "s"}.`);
    } else if (payload.job_type === "move_album") {
      setStatus(payload.message || "Album moved to library.");
    } else {
      setStatus(payload.message || "Analysis complete.");
    }
    return;
  }

  if (payload.status === "failed") {
    stopJobPolling();
    setStatus(payload.message || "Background job failed.", true);
    return;
  }

  state.pollTimer = window.setTimeout(() => {
    pollJob(jobId).catch((error) => {
      stopJobPolling();
      setStatus(error.message, true);
    });
  }, 700);
}

function beginJobTracking(jobId, statusText) {
  stopJobPolling();
  state.currentJobId = jobId;
  activityPanel.hidden = false;
  activityTitle.textContent = "Working";
  activityPhase.textContent = "queued";
  activityMessage.textContent = statusText;
  activityCount.textContent = "0 / 0";
  activityProgressBar.style.width = "0%";
  activityReports.hidden = true;
  activityReports.innerHTML = "";
  activityErrors.hidden = true;
  activityErrors.innerHTML = "";
  setStatus(statusText);
  focusActivityPanel();
  pollJob(jobId).catch((error) => {
    stopJobPolling();
    setStatus(error.message, true);
  });
}

async function processRemoteFolder(relativePath, folderName) {
  table.hidden = true;
  stats.hidden = true;
  const response = await fetch("/api/process-remote-folder", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ relative_path: relativePath }),
  });

  const payload = await parseResponse(response);
  beginJobTracking(payload.job_id, `Processing "${folderName}" on the remote share...`);
}

async function updateAlbumArtist() {
  const newAlbumArtist = albumArtistInput.value.trim();
  if (!newAlbumArtist) {
    setStatus("Enter a new album artist first.", true);
    return;
  }

  if (!state.editable || state.sourceType !== "remote" || !state.currentRelativePath) {
    setStatus("Bulk album artist editing is only available after processing a remote folder.", true);
    return;
  }

  const shouldContinue = window.confirm(
    `Change album artist to "${newAlbumArtist}" for all supported audio files in "${state.currentSource}" on the remote share?`,
  );
  if (!shouldContinue) {
    return;
  }

  const response = await fetch("/api/update-album-artist", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      relative_path: state.currentRelativePath,
      album_artist: newAlbumArtist,
    }),
  });

  const payload = await parseResponse(response);
  beginJobTracking(payload.job_id, `Updating album artist in "${state.currentSource}"...`);
}

async function updateAlbumTitle() {
  const newAlbumTitle = albumTitleInput.value.trim();
  if (!newAlbumTitle) {
    setStatus("Enter a new album title first.", true);
    return;
  }

  if (!state.editable || state.sourceType !== "remote" || !state.currentRelativePath) {
    setStatus("Bulk album title editing is only available after processing a remote folder.", true);
    return;
  }

  const shouldContinue = window.confirm(
    `Change album title to "${newAlbumTitle}" for all supported audio files in "${state.currentSource}" on the remote share?`,
  );
  if (!shouldContinue) {
    return;
  }

  const response = await fetch("/api/update-album-title", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      relative_path: state.currentRelativePath,
      album_title: newAlbumTitle,
    }),
  });

  const payload = await parseResponse(response);
  beginJobTracking(payload.job_id, `Updating album title in "${state.currentSource}"...`);
}

function setBrowserStatus(message, isError = false) {
  browserStatus.textContent = message;
  browserStatus.style.color = isError ? "#8c1c13" : "";
}

function renderBrowserEntries(entries) {
  browserList.innerHTML = "";

  if (!entries.length) {
    const empty = document.createElement("div");
    empty.className = "browser-status";
    empty.textContent = "No subfolders found here.";
    browserList.appendChild(empty);
    return;
  }

  const fragment = document.createDocumentFragment();
  entries.forEach((entry) => {
    const row = document.createElement("div");
    row.className = "browser-entry";
    const isProcessed = state.processedFolders.has(entry.relative_path);
    if (isProcessed) {
      row.classList.add("browser-entry-processed");
    }

    const info = document.createElement("div");
    const name = document.createElement("p");
    name.className = "browser-entry-name";
    name.textContent = entry.name;

    const meta = document.createElement("div");
    meta.className = "browser-meta";
    meta.textContent = `${entry.subdirectory_count} subfolder${entry.subdirectory_count === 1 ? "" : "s"} • ${entry.audio_file_count} audio file${entry.audio_file_count === 1 ? "" : "s"}`;
    info.append(name, meta);

    const actions = document.createElement("div");
    actions.className = "browser-actions";

    const openButton = document.createElement("button");
    openButton.type = "button";
    openButton.className = "button button-secondary button-small";
    openButton.textContent = "Open";
    openButton.addEventListener("click", async () => {
      try {
        await loadRemoteBrowser(entry.relative_path);
      } catch (error) {
        setBrowserStatus(error.message, true);
      }
    });

    const processButton = document.createElement("button");
    processButton.type = "button";
    processButton.className = "button button-small";
    processButton.textContent = "Process Folder";
    processButton.addEventListener("click", async () => {
      try {
        await processRemoteFolder(entry.relative_path, entry.name);
      } catch (error) {
        setStatus(error.message, true);
      }
    });

    if (isProcessed) {
      const doneBadge = document.createElement("div");
      doneBadge.className = "processed-badge";
      doneBadge.textContent = "✓ Processed";
      actions.append(openButton, doneBadge);
    } else {
      actions.append(openButton, processButton);
    }
    row.append(info, actions);
    fragment.appendChild(row);
  });

  browserList.appendChild(fragment);
}

async function loadRemoteBrowser(relativePath = "") {
  setBrowserStatus("Fetching folders...");
  const query = relativePath ? `?path=${encodeURIComponent(relativePath)}` : "";
  const response = await fetch(`/api/remote-browser${query}`);
  const payload = await parseResponse(response);

  state.browserPath = payload.current_relative_path || "";
  state.browserParentPath = payload.parent_relative_path || "";
  browserPath.textContent = payload.current_display_path;
  browserUpButton.disabled = state.browserPath === "";
  renderBrowserEntries(payload.entries || []);
  setBrowserStatus(`Showing ${payload.entries.length} folder${payload.entries.length === 1 ? "" : "s"}.`);
}

updateAlbumArtistButton.addEventListener("click", async () => {
  try {
    await updateAlbumArtist();
  } catch (error) {
    setStatus(error.message, true);
  }
});

updateAlbumTitleButton.addEventListener("click", async () => {
  try {
    await updateAlbumTitle();
  } catch (error) {
    setStatus(error.message, true);
  }
});

browserUpButton.addEventListener("click", async () => {
  try {
    await loadRemoteBrowser(state.browserParentPath);
  } catch (error) {
    setBrowserStatus(error.message, true);
  }
});

loadRemoteBrowser().catch((error) => {
  setBrowserStatus(error.message, true);
});
