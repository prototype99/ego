#!/usr/bin/python3

"""

Here's what's going on. I'm rewriting profile.ego but in a way that is independent of ego, so it can be used as a
general API for handling profiles. I am also trying to make this code production-quality -- this includes making it
very clean, easy-to-understand, well-documented with concepts clearly explained, and easy to unit-test.


"""

import json
import os
from enum import Enum
import errno
from collections import OrderedDict, defaultdict
from ego.output import Output
from ego.config import join_path, EgoConfig
from configparser import ConfigParser
import configparser


def all_funtoo_repos(config):
	dict_out = {}
	conf_in = ConfigParser()
	repo_path = join_path(config.root_path, "/etc/portage/repos.conf")
	try:
		conf_in.read(map(lambda x: repo_path + "/" + x, os.listdir(repo_path)))
	except configparser.Error as e:
		Output.error("Parse error in /etc/portage/repos.conf: %s" % e.message)
		raise e
	for repo_name in conf_in.sections():
		if repo_name == "DEFAULT":
			continue
		if "location" not in conf_in[repo_name]:
			Output.warning("No location specified for '%s' repository in %s" % (repo_name, repo_path))
		dict_out[repo_name] = {
			"has_profiles": os.path.exists(join_path(config.root_path, conf_in[repo_name]["location"] + "/profiles/profiles.ego.desc")),
			"config": conf_in[repo_name]
		}
	return dict_out

class ProfileName(Enum):
	"""
	The ``ProfileName`` and ``ProfileType`` implementation give us the following enumerations that can be compared
	against one another::

	  ProfileType.ARCH < ProfileType.BUILD < ProfileType.ARCH < ProfileType.SUBARCH < ProfileType.MIX_IN

	The order of comparison is reflective of the order that these profile types appear in within
	``/etc/portage/make.profile/parent``.

	The ProfileType enumerations also have string values that can be accessed with the ``str()`` method, and can also
	be compared against other string using equality comparison::

	  assertEqual(ProfileType.MIX_IN == "mix_ins")

	"""

	def __new__(cls, intval, strval):
		value = len(cls.__members__) + 1
		obj = object.__new__(cls)
		obj._value_ = value
		obj._intval = intval
		obj._strval = strval
		return obj

	def __ge__(self, other):
		if self.__class__ is other.__class__:
			return self._intval >= other._intval
		return NotImplemented

	def __gt__(self, other):
		if self.__class__ is other.__class__:
			return self._intval > other._intval
		return NotImplemented

	def __le__(self, other):
		if self.__class__ is other.__class__:
			return self._intval <= other._intval
		return NotImplemented

	def __lt__(self, other):
		if self.__class__ is other.__class__:
			return self._intval < other._intval
		return NotImplemented

	def __eq__(self, other):
		if self.__class__ is other.__class__:
			return self._intval == other._intval
		elif isinstance(other, str):
			return self._strval == other
		return NotImplemented

	def __str__(self):
		return self._strval

	def __hash__(self):
		return self._intval


class ProfileType(ProfileName):
	ARCH = (1, "arch")
	BUILD = (2, "build")
	SUBARCH = (3, "subarch")
	FLAVOR = (4, "flavor")
	MIX_IN = (5, "mix-ins")
	OTHER = (99, "other")

	@classmethod
	def from_string(cls, my_str):
		if my_str == "mix-in":
			# support non-plural version of "mix-ins":
			return ProfileType.MIX_IN
		for t in list(cls):
			if t._strval == my_str:
				return t
		return None

	@classmethod
	def valid(cls):
		# valid for use in setting profiles.
		return [ ProfileType.ARCH, ProfileType.BUILD, ProfileType.SUBARCH, ProfileType.FLAVOR, ProfileType.MIX_IN ]

	@classmethod
	def single(cls):
		# profile types that should only be set once.
		return [ ProfileType.ARCH, ProfileType.BUILD, ProfileType.SUBARCH, ProfileType.FLAVOR ]


class MetaProfileCatalog:
	"""
	``MetaProfileCatalog`` is intended to provide an identical API to ``ProfileCatalog``, except that it can 'see both
	Funtoo Linux profiles as well as user-created Funtoo-compatible profiles as configured in overlays and enabled via
	``/etc/ego.conf``. ``MetaProfileCatalog`` does this by wrapping a ``ProfileCatalog`` that is hooked up to official
	Funtoo Linux profiles, as well as any ``ProfileCatalog``s hooked up to overlays, and aggregating their results.

	Currently, ``MetaProfileCatalog`` just wraps the official Funtoo ``ProfileCatalog``.

	"""

	def __init__(self, config, funtoo_repos):
		self.config = config
		self.catalogs = OrderedDict()
		self.funtoo_repos = funtoo_repos
		for repo, repo_info in funtoo_repos.items():
			if not repo_info["has_profiles"]:
				continue
			pa = repo_info["config"]["location"]
			self.catalogs[repo] = ProfileCatalog(repo, config, pa + "/profiles")

	def set_arch(self, arch=None):
		for repo_name, catalog in self.catalogs.items():
			catalog.set_arch(arch=arch)

	def __getitem__(self, key, arch=None):
		return self.list(key, arch)

	def find_path(self, profile_type, name):
		name_split = name.split(":")
		if len(name_split) == 1:
			repo = "didos"
		else:
			repo, name = name_split
		if repo not in self.catalogs.keys():
			raise KeyError("Specified repository '%s' is not available." % repo)
		if repo is not None:
			return repo + ":" + self.catalogs[repo].find_path(profile_type, name)
		else:
			for repo_name, catalog in self.catalogs.items():
				my_path = repo_name + ":" + catalog.find_path(profile_type, name)
				if my_path is not None:
					return my_path
		return None

	def list(self, key, arch=None):
		for repo_name, catalog in self.catalogs.items():
			for name in catalog.list(key, arch=arch):
				yield name

	@property
	def profile_root(self):
		# This property is used by ProfileSpecifier for resolving ':foo' stuff in profiles.
		return self.config.kits_root + "/didos/profiles"

class ProfileCatalog:
	"""

	``ProfileCatalog`` will allow us to see what profile settings -- flavors, subarches, mix-ins, builds -- are
	available in a particular profile repository.
	
	It is initialized by specifying a repository directory where ``ProfileCatalog`` will find an ``ego.desc`` file. This
	JSON file will be read in and contains paths where ProfileCatalog can find each type of sub-profile.
	
	ProfileCatalog allows an ``arch`` string to be set via ``self.set_arch()`` or specified directly as a keyword
	argument to the ``self.list()`` method. When specified, this string will be used to augment the list of mix-ins with
	subarch mix-ins, and provide a list of subarches. Note that without specifying ``arch``, it is impossible for
	``ProfileCatalog`` to know where to look for subarch profiles.

	"""

	def __init__(self, repo_name, config, profile_root):
		self.config = config
		self.repo_name = repo_name
		self.profile_root = profile_root
		self.egodescfile = self.profile_root + "/profiles.ego.desc"
		self.directory_map = defaultdict(dict)
		self.json_info = {}
		if os.path.exists(self.egodescfile):
			with open(self.egodescfile, "r") as ed:
				self.json_info = json.loads(ed.read())
		self.arch = None

	# keys() returns a list of types of sub-profiles that are defined on this system.

	def set_arch(self, arch=None):
		"""Allows the arch to be set once, rather than passed as an argument to self.list."""
		self.arch = arch

	def __getitem__(self, key, arch=None):
		return self.list(key, arch)

	def find_path(self, profile_type, name):
		"""
		Returns relative path of a particular profile that we have already found via a list() call.

		:param profile_type: The ProfileType Enum of the profile.
		:param name: The literal name of the profile, such as "gnome", or "core-kit:gnome"
		:return: a relative path to the profile, or None if not found.
		"""
		try:
			return self.directory_map[profile_type][name]
		except KeyError:
			return None

	def list(self, key, arch=None):

		"""
		Yields available profiles of a particular ProfileType. For example, given a ProfileType of mix-in,
		this method may yield: "gnome", "foobar", "mymixin".

		:param key: A ProfileType specifying the ProfileType to list.
		:param arch: An arch must be specified in order to also list ``subarch`` and arch-specific mix-ins.
		:return: generator
		"""

		if not arch and self.arch:
			arch = self.arch

		dirlist = []

		if arch is not None:

			if key == ProfileType.SUBARCH:
				dirlist = [self.json_info[str(ProfileType.ARCH)] + "/" + arch + "/subarch"]
			elif key == ProfileType.MIX_IN:
				dirlist = [self.json_info[str(ProfileType.MIX_IN)] + "/" + arch + "/mix-ins"]

		if str(key) in self.json_info:
			dirlist += [self.json_info[str(key)]]

		# For now, disable defining new arches in overlays. This prevents extra arches from being displayed when
		# sub-arches are defined.

		if self.repo_name != "core-kit" and key == ProfileType.ARCH:
			return

		for dirname in dirlist:
			p = join_path(self.config.root_path, self.profile_root + "/" + dirname)
			try:
				for profile_root in os.listdir(p):
					if os.path.isdir(p + "/" + profile_root):
						self.directory_map[key][profile_root] = dirname + "/" + profile_root
						if self.repo_name != "didos":
							yield self.repo_name + ":" + profile_root
						else:
							yield profile_root
			except OSError as e:
				if e.errno not in (errno.ENOTDIR, errno.ENOENT, errno.ESTALE):
					raise
				continue


class ProfileSpecifier(object):
	""" ``ProfileSpecifier`` is an object used by ``ProfileTree`` to model the profile hierarchy. Each
	``ProfileSpecifier`` takes a specifier that is in one of the following formats:

	``:foo/bar/oni``
	  Relative to the base of the profile root.

	``foo:bar/oni``
	  Relative to the base of a particular repository.

	``[relative_path]``
	  A relative path specification that is interpreted relative to the current directory.

	  For the sake of completeness and usefulness, and not because it is really officially 'allowed' in Portage,
	  absolute paths (those starting with ``/``) are supported too.

	"""

	def __init__(self, tree, cwd, spec_str, repo_name):
		"""
		:param tree: A ``ProfileTree`` object.
		:param cwd:  The current working directory of the ``parent`` file that the specifier came from.
		:param spec_str: A single line from a ``parent`` file specifying another profile.
		:param repo_name: The 'current' repo_name. None if found in /etc/portage/make.profiles/parent; if parsed from
		inside a repo, then this will be something like 'core-kit'.
		"""

		self.tree = tree
		self.cwd = cwd
		self.spec_str = spec_str

		if self.spec_str.startswith("gentoo:"):
			self.spec_str = "core-kit:" + self.spec_str[7:]
			self.modified = True
		else:
			self.modified = False
		self._resolved_path = None
		self._profile_type = None
		self.repo_name = repo_name
		spl = self.spec_str.split(":")
		if len(spl) == 2:
			self.repo_name = spl[0]

	def __str__(self):
		return self.spec_str

	def __repr__(self):
		return "<ProfileSpecifier: %s>" % self.spec_str

	@property
	def resolved_path(self):

		if self._resolved_path is None:

			if self.spec_str[0] == ":":
				# ":base" format -- relative to root of profile directory:
				# TODO: this may not be correct -- should ":foo" be relative to the base of whatever profile we happen to be in?
				self._resolved_path = os.path.join(self.tree.master_catalog.profile_root, self.spec_str[1:])
			else:
				colsplit = self.spec_str.split(":")
				if len(colsplit) == 2 and colsplit[0] in self.tree.funtoo_repos.keys():
					# "gentoo:foo" format - relative to a specified repo:
					rel_path = self.tree.funtoo_repos[colsplit[0]]["config"]["location"]
					self._resolved_path = os.path.join(rel_path, "profiles", colsplit[1])
					# TODO: handle situation where for some reason, we have a repo entry referencing a non-existing repo
				else:
					if self.spec_str.startswith("/"):
						# absolute path
						self._resolved_path = self.spec_str
					else:
						# relative path format - relative to current location.
						self._resolved_path = os.path.join(self.cwd, self.spec_str)

			self._resolved_path = os.path.normpath(self._resolved_path)

		return self._resolved_path

	@property
	def name(self):
		if self.repo_name is None or self.repo_name == "didos":
			return self.resolved_path.split('/')[-1]
		else:
			return self.repo_name + ":" + self.resolved_path.split('/')[-1]

	def classify(self):

		"""
		This method will look at the resolved path and inspect it to see if it's a mix-in path, etc. We need this
		because the specifier doesn't tell us what type of profile it is, but that information is important to us.

		The way to tell is to look at the containing path and see if it is in a "flavor", etc. directory.

		:return: A ProfileType Enum telling us the type of profile, or None if not recognized as any particular kind.

		"""
		if self._profile_type is not None:
			return self._profile_type
		try:
			kind = self.resolved_path.split("/")[-2:-1][0]
			for ptype in list(ProfileType):
				if kind == str(ptype):
					self._profile_type = ptype
					return ptype
		except IndexError:
			pass
		return ProfileType.OTHER


class ProfileTree(object):

	"""
	The ```ProfileTree`` has the ability to look at the master profile settings, as well as the repositories, and
	process the inheritance patterns defined in ``parent`` files. This allows ``ProfileTree`` to determine what
	profiles are enabled, either directly or via inheritance. ``ProfileTree`` also provides a mechanism to write out
	new, modified master profile data in the proper format.

	The 'tree' of the ``ProfileTree`` begins with a root, which for Funtoo is going to be
	``/etc/portage/make.profile/parents``. This parents file will point to other profiles, which we will consider
	children. These child profiles may have their own children, etc. This hierarchy defines what profiles are enabled,
	and also defines the order in which various profile settings are interpreted.

	The profile hierarchy is stored in ``self.profile_heir``. It is generated as follows. The contents of
	``/etc/portage/make.profile/parents`` is parsed, and each literal profile specifier line is wrapped in a
	``ProfileSpecifier`` object. These lines become keys of the ordered dictionary, and their associated values are each
	a single ``OrderedDict`` containing lines (again, as ``ProfileSpecifiers``) of the ``parent`` file referenced by the
	original ``ProfileSpecifier`` key.
	"""

	def __init__(self, catalog, master_repo_name, config, funtoo_repos):

		self.master_catalog = catalog
		self.master_repo_name = master_repo_name
		self.funtoo_repos = funtoo_repos
		self.config = config
		self.root_parent_dir = join_path(self.config.root_path, '/etc/portage/make.profile')
		self.parent_map = defaultdict(None)
		# put variable definitions above this line ^^
		self.reload()

	def get_arch(self):
		current_arch = list(self.get_children(ProfileType.ARCH))
		if len(current_arch):
			return current_arch[0]
		return None

	def reload(self, parent_lines=None):
		self.profile_path_map = {}

		# profile_path_map: Map the absolute path of profile directory to an OrderedDict containing ProfileSpecifiers
		# for each line in the parent file of the directory it references.

		self.profile_hier = self._recurse(parent_lines=parent_lines)

	@property
	def master_parent_file(self):
		return os.path.join(self.root_parent_dir, "parent")

	@property
	def modified(self):
		for specifier, odict in self.profile_hier.items():
			if specifier.modified:
				return True
		return False

	def write(self, config: EgoConfig, outfile):

		python_kit_branch, default_branch = config.get_configured_kit("python-kit")

		# TODO: it's possible to get blank lines in the profile file, and the specifier doesn't like this...

		for specifier, odict in self.profile_hier.items():
			strout = str(specifier)
			if strout.find(":funtoo/kits/python-kit/") != -1:
				# strip old python-kit settings
				continue
			outfile.write(strout + '\n')

		# add new python-kit settings
		for kit in self.config.all_kit_names_in_release:
			python_path = os.path.join(config.kits_root, kit, "profiles/funtoo/kits/python-kit/", python_kit_branch)
			if os.path.exists(python_path):
				outfile.write("%s:funtoo/kits/python-kit/" % kit + python_kit_branch + "\n")

	def remove_line(self, spec_str):
		"""
		Remove a specified profile line.
		:param spec_str: The literal profile line string to remove.
		:return: None
		"""
		new_lines = []
		for spec_obj in self.profile_hier.keys():
			if spec_obj.spec_str != spec_str:
				new_lines.append(spec_obj.spec_str)
		self.reload(new_lines)

	def remove_name(self, profile_type, name):
		"""
		Remove a profile entry of a particular profile type and name.
		:param profile_type: The ``ProfileType`` Enum to match.
		:param name: The directory name of the profile to match (for example, 'workstation', 'gnome', etc.)
		:return: None
		"""
		new_lines = [spec_obj.spec_str for spec_obj in self.profile_hier.keys() if not ((profile_type == spec_obj.classify()) and (spec_obj.name == name))]
		self.reload(new_lines)

	def append_mixin(self, spec_str):
		"""

		The ``append_mixin()`` method will append a new mix-in line to the master parents file in- memory, preserving
		'proper' order, which in our case means appending after the last mix-in that appears in the file (we simply
		append a line at the end.)

		:param spec_str: The profile specification string that points to the mix-in to be added.
		:return: None
		"""

		new_lines = [spec_obj.spec_str for spec_obj in self.profile_hier.keys()]
		if spec_str not in new_lines:
			new_lines.append(spec_str)

		self.reload(new_lines)

	def insert_or_replace_entry(self, profile_type, spec_str):

		"""
		The ``insert_or_replace_entry()`` method will replace the first found ProfileSpecifier of type ``profile_type`` with the
		new ProfileSpecifier ``spec_obj``. Use this to change flavors, subarches, etc. (single-use profiles.)

		If the replacement fails, since the profile type does not exist, it will be inserted at the correct position in the file.

		This method will throw a KeyError when a mix-in is specified.

		:param profile_type: The ``ProfileType`` Enum specifying the profile type.
		:param spec_str: The literal profile line (string) to be used to replace the existing one, or insert.
		:return: None
		"""

		new_lines = []

		added = False
		line_types = []

		for key_spec, odict in self.profile_hier.items():
			if key_spec.classify() == profile_type:
				added = True
				new_lines.append(spec_str)
			else:
				new_lines.append(key_spec.spec_str)
			line_types.append(key_spec)

		if not added:
			if profile_type == ProfileType.ARCH:
				insert_pos = 0
			elif profile_type == ProfileType.FLAVOR:
				try:
					insert_pos = next(i for i,v in enumerate(line_types) if v.classify() == ProfileType.BUILD) + 1
				except StopIteration:
					try:
						# before first mix-in
						insert_pos = next(i for i, v in enumerate(line_types) if v.classify() == ProfileType.MIX_IN)
					except StopIteration:
						try:
							# insert after subarch:
							insert_pos = next(i for i, v in enumerate(line_types) if v.classify() == ProfileType.SUBARCH) + 1
						except StopIteration:
							try:
								# insert after arch:
								insert_pos = next(i for i, v in enumerate(line_types) if v.classify() == ProfileType.ARCH) + 1
							except StopIteration:
								insert_pos = 0
			elif profile_type == ProfileType.BUILD:
				try:
					# insert before flavor:
					insert_pos = next(i for i, v in enumerate(line_types) if v.classify() == ProfileType.FLAVOR)
				except StopIteration:
					try:
						# insert after subarch:
						insert_pos = next(i for i, v in enumerate(line_types) if v.classify() == ProfileType.SUBARCH) + 1
					except StopIteration:
						try:
							# insert after arch:
							insert_pos = next(i for i, v in enumerate(line_types) if v.classify() == ProfileType.ARCH) + 1
						except StopIteration:
							insert_pos = 0
			elif profile_type == ProfileType.SUBARCH:
				try:
					# insert after arch:
					insert_pos = next(i for i, v in enumerate(line_types) if v.classify() == ProfileType.ARCH) + 1
				except StopIteration:
					try:
						# insert before build:
						insert_pos = next(i for i, v in enumerate(line_types) if v.classify() == ProfileType.BUILD)
					except StopIteration:
						insert_pos = 0
			else:
				raise KeyError(message="I do not not support profile type %s" % repr(profile_type))
			new_lines.insert(insert_pos, spec_str)
		self.reload(new_lines)

	def get_parent(self, spec_obj):
		"""
		Given a profile specifier, this method returns the parent of this profile specifier, or ``None`` if no parent.
		:param spec_obj: Profile specifier object.
		:return: Parent, or ``None``.
		"""
		return self.parent_map[spec_obj]

	def _recurse(self, profile_path=None, parent_lines=None, _parent=None, repo_name=None):

		"""
		Called by the ``reload()`` method (which is called by the constructor too), this method recurses over the master
		parent file and loads a hierarchy of profile settings. Alternatively, one can specify profile lines using the
		``parent_lines`` variable, in which case, these values are used instead (the ``parent_lines`` approach is used
		by other methods to  *change* the master profile by specifying slightly different lines than are actually in the
		parent file.)


		:param profile_path: A specified profile path to use, or ``/etc/portage/make.profile`` if None.
		:param parent_lines: If None, use the file on disk; otherwise, use the specified lines instead.
		:param _parent: a ProfileSpecifier created by a prior ``_recurse()`` call, or None if we are starting recursion.
		:return: An ``OrderedDict`` of ``ProfileSpecifier`` / ``OrderedDict`` pairs.
		"""

		res_path = profile_path.resolved_path if profile_path is not None else self.root_parent_dir
		if res_path in self.profile_path_map:
			# we've already scanned this profile. Nice side-effect of preventing infinite loops.
			return self.profile_path_map[res_path]

		new_children = OrderedDict()
		if parent_lines is None:
			parent_lines = self._read_parent(res_path)
		for spec_str in parent_lines:
			spec_obj = ProfileSpecifier(self, res_path, spec_str, repo_name)
			self.parent_map[spec_obj] = _parent
			new_children[spec_obj] = self._recurse(spec_obj, _parent=spec_obj, repo_name=spec_obj.repo_name)
		self.profile_path_map[res_path] = new_children
		return new_children

	def get_children(self, child_types=None, specifier=None):

		"""
		This method will return all immediate children of a particular profile type or types.

		:param specifier: ``ProfileSpecifier`` in hierarchy to look, or root of hierarchy if None
		:param child_types: a list of ``ProfileType``\s to collect in a list. If ``None``, collect all.
		:return:  A list of ``ProfileSpecifier``\s matching the criteria.
		"""

		# get OrderedDict of profile items defined at this particular profile location:
		child_dict = self.profile_path_map[specifier.resolved_path if specifier else self.root_parent_dir]
		# now iterate through each profile item defined:
		for child_path, child_target_dict in child_dict.items():
			if child_types is None:
				# None means "yield all"
				yield child_path
			elif isinstance(child_types, ProfileType):
				if child_path.classify() == child_types:
					yield child_path
			elif child_path.classify() in child_types:
				# Otherwise, a list and we match all specified types:
				yield child_path

	def recursively_get_children(self, child_types=None, specifier=None, _child_dict=None):

		"""
		This method will recursively scan the profile hierarchy for all enabled profiles of a particular type or types.
		A list of ``ProfileSpecifier`` objects will be returned.

		:param child_types: A list of ``ProfileType``\s to scan for, or ``None`` to return all types. Or just a single ``ProfileType``.
		:param specifier: Start at the specified ``ProfileSpecifier`` in the hierarchy, or at top if ``None``.
		:param _child_dict: Used for recursion calls only.
		:return: A list of ``ProfileSpecifier`` objects matching the criteria.

		"""
		_child_dict = _child_dict if _child_dict is not None else self.profile_path_map[
			specifier.resolved_path if specifier else self.root_parent_dir]
		out = []
		for child_path, child_target_dict in _child_dict.items():
			if child_types is None:
				out.append(child_path)
			elif isinstance(child_types, ProfileType):
				if child_path.classify() == child_types:
					out.append(child_path)
			elif child_path.classify() in child_types:
				out.append(child_path)
			out += self.recursively_get_children(child_types, child_path, _child_dict=child_target_dict)
		return out

	def _read_parent(self, parent_dir):
		fn = os.path.join(parent_dir, "parent")
		if not os.path.exists(fn):
			return
		with open(fn, 'r') as f:
			for line in f.readlines():
				if len(line) and line[0] == "#":
					continue
				elif len(line) == 0:
					continue
				yield line.strip()


def getProfileCatalogAndTree(config):
	funtoo_repos = all_funtoo_repos(config)
	catalog = MetaProfileCatalog(config, funtoo_repos)
	tree = ProfileTree(catalog, "didos", config, funtoo_repos)
	current_arch = tree.get_arch()
	catalog.set_arch(current_arch.name if current_arch is not None else None)
	return catalog, tree

# vim: ts=4 noet sw=4
