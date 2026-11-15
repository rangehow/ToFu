//! Semantic workspace service and explicit Git exchange using libgit2.
//!
//! Native revision IDs stay distinct from Git commit IDs. No Git executable
//! is spawned by this module.

use std::io;
use std::path::{Path, PathBuf};

use git2::{Commit, ObjectType, Repository, Signature, TreeWalkResult};
use serde::{Deserialize, Serialize};

use crate::{Capture, Entry, Manifest, Result, Workspace, WorkspaceError};

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct RevisionInfo {
    pub revision_id: String,
    pub git_commit: Option<String>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct CaptureResult {
    pub revision: RevisionInfo,
    pub changed_paths: Vec<String>,
    pub content_digest: String,
}

pub trait WorkspaceService {
    fn import_git(&mut self, repository: &Path, commit: &str) -> Result<RevisionInfo>;
    fn export_git(&mut self, repository: &Path, revision: &str, branch: &str) -> Result<String>;
    fn capture(&mut self, changed_paths: Option<Vec<String>>) -> Result<CaptureResult>;
    fn materialize(&mut self, revision: &str) -> Result<()>;
}

pub struct NativeWorkspaceService {
    root: PathBuf,
    workspace: Workspace,
}

fn exchange_error(detail: impl Into<String>) -> WorkspaceError {
    WorkspaceError::Io(io::Error::new(io::ErrorKind::InvalidData, detail.into()))
}

fn git_error(error: git2::Error) -> WorkspaceError {
    WorkspaceError::Io(io::Error::new(
        io::ErrorKind::InvalidData,
        error.to_string(),
    ))
}

fn validate_path(path: &str) -> Result<()> {
    if path.is_empty()
        || path.starts_with('/')
        || path
            .split('/')
            .any(|part| part.is_empty() || matches!(part, "." | ".." | ".git" | ".tofu-workspace"))
        || path.contains('\0')
    {
        return Err(exchange_error(format!("unsafe Git path: {path:?}")));
    }
    Ok(())
}

fn revision<'a>(repository: &'a Repository, name: &str) -> Result<Commit<'a>> {
    let object = repository.revparse_single(name).map_err(git_error)?;
    object.peel_to_commit().map_err(git_error)
}

impl NativeWorkspaceService {
    pub fn create(root: impl AsRef<Path>) -> Result<Self> {
        let root = root.as_ref().to_path_buf();
        let workspace = Workspace::create(&root)?;
        Ok(Self { root, workspace })
    }

    pub fn open(root: impl AsRef<Path>) -> Result<Self> {
        let root = root.as_ref().to_path_buf();
        let workspace = Workspace::open(&root)?;
        Ok(Self { root, workspace })
    }

    pub fn current_revision(&self) -> Option<RevisionInfo> {
        self.workspace.head("main").map(|revision_id| RevisionInfo {
            revision_id,
            git_commit: None,
        })
    }

    pub fn manifest(&self, revision: &str) -> Result<Manifest> {
        self.workspace.entries(revision)
    }

    fn content_digest(entries: &Manifest) -> String {
        use sha2::{Digest, Sha256};
        let mut hasher = Sha256::new();
        for (path, entry) in entries {
            hasher.update(path.as_bytes());
            hasher.update([0]);
            hasher.update(entry.kind.as_bytes());
            hasher.update(entry.content.as_bytes());
            hasher.update([entry.executable as u8]);
        }
        format!("{:x}", hasher.finalize())
    }

    pub fn import_git_into_new_root(
        root: impl AsRef<Path>,
        repository: &Path,
        commit: &str,
    ) -> Result<(Self, RevisionInfo)> {
        let mut service = Self::create(root)?;
        let revision = service.import_git(repository, commit)?;
        Ok((service, revision))
    }

    fn import_manifest(&mut self, entries: Manifest) -> Result<String> {
        self.workspace.import_manifest(entries)
    }
}

fn build_tree<'a>(
    repository: &'a Repository,
    entries: &Manifest,
    prefix: &str,
    workspace: &Workspace,
) -> Result<git2::Tree<'a>> {
    let mut builder = repository.treebuilder(None).map_err(git_error)?;
    let mut directories = std::collections::BTreeSet::new();
    for (path, entry) in entries {
        let Some(relative) = path.strip_prefix(prefix) else {
            continue;
        };
        if let Some((directory, _)) = relative.split_once('/') {
            directories.insert(directory.to_string());
            continue;
        }
        let bytes = repository
            .blob(&workspace.object_bytes(&entry.content)?)
            .map_err(git_error)?;
        let mode = match (entry.kind.as_str(), entry.executable) {
            ("file", true) => git2::FileMode::BlobExecutable,
            ("file", false) => git2::FileMode::Blob,
            ("symlink", _) => git2::FileMode::Link,
            _ => return Err(exchange_error("unsupported native entry kind")),
        };
        builder
            .insert(relative, bytes, mode.into())
            .map_err(git_error)?;
    }
    for directory in directories {
        let child_prefix = format!("{prefix}{directory}/");
        let subtree = build_tree(repository, entries, &child_prefix, workspace)?;
        builder
            .insert(&directory, subtree.id(), git2::FileMode::Tree.into())
            .map_err(git_error)?;
    }
    let id = builder.write().map_err(git_error)?;
    repository.find_tree(id).map_err(git_error)
}

impl WorkspaceService for NativeWorkspaceService {
    fn import_git(&mut self, repository_path: &Path, commit_name: &str) -> Result<RevisionInfo> {
        if self.current_revision().is_some() || self.root.join(".git").exists() {
            return Err(exchange_error("Git import requires a new native root"));
        }
        let repository = Repository::open(repository_path).map_err(git_error)?;
        let commit = revision(&repository, commit_name)?;
        let tree = commit.tree().map_err(git_error)?;
        let mut entries = Manifest::new();
        let mut error = None;
        tree.walk(git2::TreeWalkMode::PreOrder, |directory, entry| {
            let name = entry.name().unwrap_or_default();
            let path = format!("{directory}{name}");
            if let Err(failure) = validate_path(&path) {
                error = Some(failure);
                return TreeWalkResult::Abort;
            }
            let mode = entry.filemode();
            let (kind, executable) = match mode {
                0o100644 => ("file", false),
                0o100755 => ("file", true),
                0o120000 => ("symlink", false),
                0o040000 => return TreeWalkResult::Ok,
                _ => {
                    error = Some(exchange_error(
                        "submodules and special Git entries are not admitted",
                    ));
                    return TreeWalkResult::Abort;
                }
            };
            let object = match repository.find_object(entry.id(), Some(ObjectType::Blob)) {
                Ok(object) => object,
                Err(failure) => {
                    error = Some(git_error(failure));
                    return TreeWalkResult::Abort;
                }
            };
            let Some(blob) = object.as_blob() else {
                error = Some(exchange_error("tree entry is not a blob"));
                return TreeWalkResult::Abort;
            };
            let id = match self.workspace.import_object(blob.content().to_vec()) {
                Ok(id) => id,
                Err(failure) => {
                    error = Some(failure);
                    return TreeWalkResult::Abort;
                }
            };
            entries.insert(
                path,
                Entry {
                    kind: kind.into(),
                    content: id,
                    executable,
                },
            );
            TreeWalkResult::Ok
        })
        .map_err(git_error)?;
        if let Some(error) = error {
            return Err(error);
        }
        let revision_id = self.import_manifest(entries)?;
        Ok(RevisionInfo {
            revision_id,
            git_commit: Some(commit.id().to_string()),
        })
    }

    fn export_git(
        &mut self,
        repository_path: &Path,
        revision_id: &str,
        branch: &str,
    ) -> Result<String> {
        if branch.is_empty()
            || branch.starts_with('/')
            || branch.ends_with('/')
            || branch.contains("..")
            || branch.contains("//")
            || branch.contains(char::is_whitespace)
            || branch.contains('~')
            || branch.contains('^')
            || branch.contains(':')
            || branch.contains('?')
            || branch.contains('*')
            || branch.contains('[')
            || branch.contains('\\')
        {
            return Err(exchange_error("invalid export branch"));
        }
        let repository = Repository::open(repository_path).map_err(git_error)?;
        let entries = self.workspace.entries(revision_id)?;
        let tree = build_tree(&repository, &entries, "", &self.workspace)?;
        let signature = Signature::now("Tofu Native Workspace", "native-workspace@localhost")
            .map_err(git_error)?;
        let message = "Tofu native workspace export";
        let commit = repository
            .commit(None, &signature, &signature, message, &tree, &[])
            .map_err(git_error)?;
        repository
            .reference(
                &format!("refs/heads/{branch}"),
                commit,
                true,
                "tofu native export",
            )
            .map_err(git_error)?;
        Ok(commit.to_string())
    }

    fn capture(&mut self, changed_paths: Option<Vec<String>>) -> Result<CaptureResult> {
        let before = self
            .workspace
            .head("main")
            .map(|revision| self.workspace.entries(&revision))
            .transpose()?
            .unwrap_or_default();
        let capture = match changed_paths {
            Some(paths) => Capture::ChangedPaths(paths),
            None => Capture::Complete,
        };
        let revision = self.workspace.commit(capture, "main")?;
        let after = self.workspace.entries(&revision)?;
        let changed_paths = crate::diff(&before, &after)
            .into_iter()
            .map(|status| status.path)
            .collect();
        Ok(CaptureResult {
            revision: RevisionInfo {
                revision_id: revision,
                git_commit: None,
            },
            changed_paths,
            content_digest: Self::content_digest(&after),
        })
    }

    fn materialize(&mut self, revision: &str) -> Result<()> {
        self.workspace.checkout(revision)
    }
}
