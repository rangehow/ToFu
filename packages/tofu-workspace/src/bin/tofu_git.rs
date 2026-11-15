//! Native `tofu-git` command-line surface backed by libgit2.
//!
//! This is an explicit compatibility subset, not a claim of complete Git
//! compatibility. It never falls back to a system `git` executable.

use std::fs;
use std::io::{self, Write};
use std::path::{Path, PathBuf};

use git2::{Commit, Repository, Signature, StatusOptions};

fn error(message: impl Into<String>) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message.into())
}

fn repository(path: &Path) -> io::Result<Repository> {
    Repository::discover(path).map_err(|failure| error(failure.to_string()))
}

fn usage() -> ! {
    eprintln!("usage: tofu-git [-C path] init|status|add|commit|log|diff|rev-parse|checkout ...");
    std::process::exit(2)
}

fn identity() -> io::Result<Signature<'static>> {
    let name = std::env::var("GIT_AUTHOR_NAME").unwrap_or_else(|_| "Tofu User".into());
    let email = std::env::var("GIT_AUTHOR_EMAIL").unwrap_or_else(|_| "tofu@localhost".into());
    Signature::now(&name, &email).map_err(|failure| error(failure.to_string()))
}

fn print_status(repository: &Repository) -> io::Result<()> {
    let mut options = StatusOptions::new();
    options.include_untracked(true).recurse_untracked_dirs(true);
    let statuses = repository
        .statuses(Some(&mut options))
        .map_err(|failure| error(failure.to_string()))?;
    for entry in statuses.iter() {
        let flags = entry.status();
        let path = entry.path().unwrap_or("<non-utf8>");
        let code = if flags.contains(git2::Status::INDEX_NEW) {
            "A "
        } else if flags.contains(git2::Status::INDEX_MODIFIED | git2::Status::INDEX_DELETED) {
            "M "
        } else if flags.contains(git2::Status::WT_NEW) {
            "??"
        } else {
            " M"
        };
        println!("{code} {path}");
    }
    Ok(())
}

fn commit(repository: &Repository, message: &str) -> io::Result<String> {
    let signature = identity()?;
    let tree = repository
        .index()
        .and_then(|mut index| index.write_tree_to(repository))
        .map_err(|failure| error(failure.to_string()))?;
    let tree = repository
        .find_tree(tree)
        .map_err(|failure| error(failure.to_string()))?;
    let parents = match repository.head() {
        Ok(head) => vec![head
            .peel_to_commit()
            .map_err(|failure| error(failure.to_string()))?],
        Err(_) => Vec::new(),
    };
    let references: Vec<&Commit> = parents.iter().collect();
    let id = repository
        .commit(
            Some("HEAD"),
            &signature,
            &signature,
            message,
            &tree,
            &references,
        )
        .map_err(|failure| error(failure.to_string()))?;
    Ok(id.to_string())
}

fn checkout(repository: &Repository, name: &str) -> io::Result<()> {
    let object = repository
        .revparse_single(name)
        .map_err(|failure| error(failure.to_string()))?;

    repository
        .checkout_tree(&object, None)
        .map_err(|failure| error(failure.to_string()))?;
    if let Ok(branch) = repository.find_branch(name, git2::BranchType::Local) {
        let reference = branch.into_reference().name().map(str::to_owned);
        repository
            .set_head(reference.as_deref().unwrap_or("HEAD"))
            .map_err(|failure| error(failure.to_string()))?;
    } else {
        repository
            .set_head_detached(object.id())
            .map_err(|failure| error(failure.to_string()))?;
    }
    Ok(())
}

fn run(args: &[String]) -> io::Result<()> {
    let mut index = 0;
    let mut directory = std::env::current_dir()?;
    while index < args.len() && args[index] == "-C" {
        index += 1;
        directory = PathBuf::from(args.get(index).ok_or_else(|| error("-C requires a path"))?);
        index += 1;
    }
    let command = args
        .get(index)
        .map(String::as_str)
        .unwrap_or_else(|| usage());
    index += 1;
    match command {
        "init" => {
            let repository =
                Repository::init(&directory).map_err(|failure| error(failure.to_string()))?;
            println!(
                "Initialized empty Git repository in {}",
                repository.path().display()
            );
            Ok(())
        }
        "status" => print_status(&repository(&directory)?),
        "add" => {
            let repository = repository(&directory)?;
            let mut git_index = repository
                .index()
                .map_err(|failure| error(failure.to_string()))?;
            let workdir = repository
                .workdir()
                .ok_or_else(|| error("bare repository"))?;
            for path in args.iter().skip(index) {
                if path.starts_with('-') {
                    return Err(error("options after add are not supported"));
                }
                let relative = Path::new(path);
                if relative.is_absolute()
                    || relative
                        .components()
                        .any(|part| matches!(part, std::path::Component::ParentDir))
                {
                    return Err(error("path escapes repository"));
                }
                let absolute = workdir.join(relative);
                if !absolute.exists() {
                    git_index
                        .remove_path(relative)
                        .map_err(|failure| error(failure.to_string()))?;
                } else {
                    git_index
                        .add_path(relative)
                        .map_err(|failure| error(failure.to_string()))?;
                }
            }
            git_index
                .write()
                .map_err(|failure| error(failure.to_string()))
        }
        "commit" => {
            if args.get(index).map(String::as_str) != Some("-m") {
                return Err(error("commit requires -m message"));
            }
            let message = args
                .get(index + 1)
                .ok_or_else(|| error("missing commit message"))?;
            let repository = repository(&directory)?;
            let id = commit(&repository, message)?;
            println!("{id}");
            Ok(())
        }
        "rev-parse" => {
            let repository = repository(&directory)?;
            for name in &args[index..] {
                let object = repository
                    .revparse_single(name)
                    .map_err(|failure| error(failure.to_string()))?;
                println!("{}", object.id());
            }
            Ok(())
        }
        "log" => {
            let repository = repository(&directory)?;
            let mut walk = repository
                .revwalk()
                .map_err(|failure| error(failure.to_string()))?;
            walk.push_head()
                .map_err(|failure| error(failure.to_string()))?;
            for id in walk {
                let id = id.map_err(|failure| error(failure.to_string()))?;
                let commit = repository
                    .find_commit(id)
                    .map_err(|failure| error(failure.to_string()))?;
                println!("commit {id}\n\n    {}", commit.summary().unwrap_or(""));
            }
            Ok(())
        }
        "checkout" => {
            let name = args
                .get(index)
                .ok_or_else(|| error("checkout requires a branch or revision"))?;
            checkout(&repository(&directory)?, name)
        }
        "diff" => {
            let repository = repository(&directory)?;
            let tree = match repository.head().and_then(|head| head.peel_to_tree()) {
                Ok(tree) => Some(tree),
                Err(_) => None,
            };
            let diff = repository
                .diff_tree_to_workdir_with_index(tree.as_ref(), None)
                .map_err(|failure| error(failure.to_string()))?;
            diff.print(git2::DiffFormat::Patch, |_, _, line| {
                print!("{}", std::str::from_utf8(line.content()).unwrap_or(""));
                true
            })
            .map_err(|failure| error(failure.to_string()))
        }
        "shim" => {
            let destination = args
                .get(index)
                .ok_or_else(|| error("shim requires a directory"))?;
            fs::create_dir_all(destination)?;
            #[cfg(unix)]
            std::os::unix::fs::symlink(
                std::env::current_exe()?,
                Path::new(destination).join("git"),
            )
            .map_err(|failure| error(failure.to_string()))?;
            println!("Prepend {} to PATH to select tofu-git", destination);
            Ok(())
        }
        _ => Err(error(format!("unsupported tofu-git command: {command}"))),
    }
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if let Err(failure) = run(&args) {
        let _ = writeln!(io::stderr(), "tofu-git: {failure}");
        std::process::exit(1);
    }
}
