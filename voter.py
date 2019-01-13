#!/usr/bin/env python3
import os
import yaml
import json
import logging
import logging.config
import argparse
import asana
import itertools


with open('logging.yaml') as fobj:
    logging.config.dictConfig(yaml.load(fobj))


logger = logging.getLogger("voter")
client = asana.Client.access_token(os.environ["ASANA_PERSONAL_ACCESS_TOKEN"])

class AsanaObj:
    def __init__(self, obj_id):
        self._obj_id = obj_id
        self._json = None

    @property
    def id(self):
        return self._obj_id

    @property
    def name(self):
        return self._get_json()['name']

    def _get_json(self):
        if not self._json:
            self._json = self._asana_request()
        return self._json

class AsanaProject(AsanaObj):
    def project_link(self):
        return f"https://app.asana.com/0/{self._obj_id}/list"

class SourceTask(AsanaObj):
    def __init__(self, task_id):
        super(SourceTask, self).__init__(task_id)

    def _asana_request(self):
        return client.tasks.find_by_id(self._obj_id, fields=['name'])

    def __str__(self):
        return f"Task {self.name} ({self.id})"

class MetaStorage(AsanaObj):
    """Persistence class that stores data in Asana's external field
    in a particular task. Both stores YAML info in the external field (for this
    script to consume) and in the task description (for humans to consume)."""

     
    @classmethod
    def create(klass, project_id):
        bootstrap_external_info = {"version": "1.0"}
        metatask = client.tasks.create({"name": "Meta task (storing persistent info)", 
            "projects": [project_id]
            })
        ms = MetaStorage(metatask['id'], project_id)
        ms.external = bootstrap_external_info
        ms.flush()
        return ms

    def __init__(self, task_id, project_id):
        super(MetaStorage, self).__init__(task_id)
        self._project_id = project_id
        self._data = None

    def is_meta_task(self):
        return self.external != None

    def external_data_key(self):
        return f"python_metadata_{self._project_id}"

    @property
    def external(self):
        if not self._data:
            data = self._get_json()
            if 'external' not in data:
                return None
            if data['external']['id'] != self.external_data_key():
                return None
            self._data = yaml.load(data['external']['data'])
        return self._data


    @external.setter
    def external(self, data):
        """ Set the "local" data. You may want to call flush() after this to write to Asana."""
        self._data = data

    def flush(self):
        y = yaml.dump(self._data)
        client.tasks.update(self._obj_id, {"notes": y,
            "external": {
                "id": self.external_data_key(),
                "data": y
                }
            })
        self._json = None
        self._data = None

    @property
    def version(self):
        if self.external == None:
            return None
        return self.external['version']

    def _asana_request(self):
        return client.tasks.find_by_id(self._obj_id, fields=['name', 'external'])

    def __str__(self):
        return f"MetaStorage: ({self.id})\n{yaml.dump(self.external)}"

class Voter(AsanaObj):
    def __init__(self, user_id):
        super(Voter, self).__init__(user_id)
        self._user_task_list_url = None

    def _asana_request(self):
        return client.users.find_by_id(self._obj_id, fields=['name'])

    def __str__(self):
        return f"User {self.name} ({self.id})"

    def task_list_user_link_url(self, obj_has_workspace_id):
        # TODO: this will someday be a full client library method;
        # Here it's just an (approved) workaround until the client libraries get updated.
        if not self._user_task_list_url:
            task_list = client.get(f"/users/{self.id}/user_task_list", {"workspace": obj_has_workspace_id._get_json()['workspace']['id']})
            # For some reason, LunaText parses the final single quote as part of the url, so insert extra space.
            self._user_task_list_url = f"https://app.asana.com/0/{task_list['id']}/list "
        return self._user_task_list_url
        

class SourceProject(AsanaProject):
    def __init__(self, project_id):
        self._source_tasks = None
        self._meta_task = None
        super(SourceProject, self).__init__(project_id)

    def _asana_request(self):
        return client.projects.find_by_id(self._obj_id, fields=['name', 'followers', 'team', 'workspace'])

    def __str__(self):
        return f"Source project {self.name} ({self.id})"


    def meta_task(self):
        """Find or create the meta task on the project"""
        # The task that is this task, but is not recognized yet (the Python task to pop).
        if self._meta_task is None:
            local_meta_task = None
            for non_cast_task in self.tasks[:]:
                """Iterate over copy"""
                local_meta_task = MetaStorage(non_cast_task.id, self.id)
                if local_meta_task.is_meta_task():
                    """Found it. Remove and return."""
                    self.tasks.remove(non_cast_task)
                    self._meta_task = local_meta_task
                    break
            if self._meta_task:
                logger.debug(f"Found meta task: {self._meta_task}")
            if not self._meta_task:
                logger.debug("Creating new metastorage task")
                self._meta_task = MetaStorage.create(p.id)
        return self._meta_task

    @property
    def tasks(self):
        """Get the project task as SourceTasks. Memoized."""
        """TODO: If/when we run this "live" we should listen for events; probably just blow away
        cache at that point and do a rescan, since the size of the project should remain small.
        Or else people will be voting forever."""
        # TODO: this has to be called after meta task popped :/ remove this implicit dependency.
        if not self._source_tasks:
            task_generator = client.tasks.find_by_project(self._obj_id, fields=['name'])
            self._source_tasks = [SourceTask(task_json['id']) for task_json in task_generator]
        return self._source_tasks

    @property
    def voters(self):
        return [Voter(follower_json['id']) for follower_json in self._get_json()['followers']]

    @property
    def team_id(self):
        return self._get_json()['team']['id']
    
    def __str__(self):
        return f"Project {self.name} ({self.id})"

class DestTask(AsanaObj):
    @classmethod
    def create(klass, source_task_one, source_task_two):
        pass

    def __init__(self):
        super(DestTask, self).__init__(project_id)


class DestProject(AsanaProject):
    @classmethod
    def create(klass, source_project, voter):
        project = client.projects.create({"name": f"Voting project from {source_project.name}", 
            "owner": voter.id,
            "team": source_project.team_id,
            "notes": "Voter project for {voter.name}; original project {source_project.project_link}"
            })
        return DestProject(project['id'], voter)

    def __init__(self, project_id, voter):
        super(DestProject, self).__init__(project_id)
        self._voter = voter

    def _asana_request(self):
        return client.projects.find_by_id(self._obj_id, fields=['name', 'followers', 'team', 'workspace'])

    @property
    def voter(self):
        return self._voter

    def current_voting_tasks(self):
        """Find (memoized) all the current voting tasks."""
        pass

    def find_voting_task_by_task_ids(self, id_one, id_two):
        """Search the voting tasks for the one with the indices given, or None if does not exist."""
        pass

    def combinations(self, source_project):
        # Ensure metatask is popped; see TODO in source project#tasks
        mt = source_project.meta_task()
        """Generate all combos of tasks"""
        combos = [p for p in itertools.combinations([t.id for t in source_project.tasks], 2)]
        logger.info(combos)

    def __str__(self):
        return f"Dest project {self.name} ({self.id}) for {self.voter}"



if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Make projects for comparison counted voting.")
    parser.add_argument("-p", "--project", help="The source project. Each task will be compared with each other.")
    parser.add_argument("-g", "--generate", help="Generate projects for each voter if needed", action='store_true')
    args = parser.parse_args()
    logger.debug(args)

    if 'project' in args:
        p = SourceProject(args.project)
        mt = p.meta_task()
        logger.info(f"Source project: {p}")
        logger.info(f"Voters: {[str(v) for v in p.voters]}")


    if 'generate' in args:
        if 'voter_projects' not in mt.external:
            logger.info("Creating voter projects record in meta")
            mt.external['voter_projects'] = {}
            mt.flush()
        dest_projects = []
        for v in p.voters:
            if v.id not in mt.external['voter_projects']:
                logger.info(f"Creating voter project for {v}")
                dest = DestProject.create(p, v)
                project_data = {
                        "id": dest.id,
                        "voter_link": v.task_list_user_link_url(dest),
                        "project_link": dest.project_link()
                        }
                mt.external['voter_projects'][v.id] = project_data
                mt.flush()
            else:
                dest = DestProject(mt.external['voter_projects'][v.id]['id'], v)
            dest_projects.append(dest)
            logger.info(f"Voting project for {v}: {dest}")

        logger.info(f"Projects {[str(d) for d in dest_projects]}")

